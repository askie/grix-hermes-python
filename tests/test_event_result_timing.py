"""事件收口时机单元测试。

对齐 connector 语义：event_result 必须在任务真正结束时按真实结果发出，
不能在消息刚派发（handle_message 返回）时就报 responded。

归属模型：open（已登记未归属）→ pending 被消费时移交 next_run / 当前轮
running → on_processing_start 认领 → on_processing_complete 按结果收口。

覆盖：
1. 任务开始不发结果、结束按结果发（responded / failed / canceled）；
2. 排队消息被消费（pending pop）时归属移交，合并丢失 event_id 也能收口；
3. 认领只取归属本轮的事件，排队中/防抖中的未来轮事件不被提前收口；
4. 撤回的触发消息上报 canceled/revoked；
5. 旁路消化的遗留事件按自身结果报 responded（不跟随本轮失败结局），
   派发途中(inflight)的事件不被误收。

走 stub 模式（同 test_final_reply_quote.py），不依赖 hermes-agent host。
"""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "tools" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        reg = types.ModuleType("tools.registry")

        class _Registry:
            def register(self, **kw):
                pass

        reg.registry = _Registry()
        reg.tool_error = lambda msg: f"ERR:{msg}"
        reg.tool_result = lambda obj: f"OK:{obj}"
        tools_pkg.registry = reg
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg

    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw_cfg = types.ModuleType("gateway.config")

        class _Platform:
            def __init__(self, name):
                self.value = name

            def __eq__(self, other):
                return getattr(other, "value", None) == self.value

            def __hash__(self):
                return hash(self.value)

        gw_cfg.Platform = _Platform
        gw_cfg.PlatformConfig = lambda **kw: SimpleNamespace(**kw)

        gw_session = types.ModuleType("gateway.session")
        gw_session.build_session_key = lambda *a, **kw: "k"

        gw_platforms = types.ModuleType("gateway.platforms")
        gw_platforms_base = types.ModuleType("gateway.platforms.base")
        gw_platforms_base.BasePlatformAdapter = object
        gw_platforms_base.MessageEvent = type("MessageEvent", (), {})
        gw_platforms_base.MessageType = type("MessageType", (), {"TEXT": "text"})
        gw_platforms_base.ProcessingOutcome = type(
            "ProcessingOutcome", (), {"SUCCESS": object(), "CANCELLED": object()}
        )
        gw_platforms_base.SendResult = type("SendResult", (), {})

        gw_run = types.ModuleType("gateway.run")
        gw_run._gateway_runner_ref = lambda: None

        sys.modules["gateway"] = gw
        sys.modules["gateway.config"] = gw_cfg
        sys.modules["gateway.session"] = gw_session
        sys.modules["gateway.platforms"] = gw_platforms
        sys.modules["gateway.platforms.base"] = gw_platforms_base
        sys.modules["gateway.run"] = gw_run


_install_stubs()

from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402

for _name in ("SUCCESS", "CANCELLED"):
    if not hasattr(adapter_mod.ProcessingOutcome, _name):
        try:
            setattr(adapter_mod.ProcessingOutcome, _name, object())
        except (AttributeError, TypeError):
            adapter_mod.ProcessingOutcome = type(
                "ProcessingOutcome",
                (),
                {"SUCCESS": object(), "CANCELLED": object()},
            )
            break


class FakeTransportClient:
    """记录 complete_event 调用的最小 transport 假件。"""

    def __init__(self):
        self._config = SimpleNamespace(shared_owner_id=None)
        self.status = {"connected": True, "authed": True}
        self.completed = []

    async def complete_event(self, *, event_id, status, code=None, message=None, updated_at=None):
        self.completed.append({"event_id": event_id, "status": status, "message": message})


def _make_adapter(client=None):
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.config = SimpleNamespace(extra={})
    inst.connection = GrixConnectionConfig(endpoint="ws://x", agent_id="100", api_key="k")
    from collections import defaultdict
    inst._owner_states = defaultdict(adapter_mod._OwnerState)
    inst._shared_clients = {}
    inst._shutting_down = False
    inst._disconnect_requested = False
    inst._client = client or FakeTransportClient()
    inst._pending_messages = adapter_mod._PendingMessagesDict(inst._on_pending_consumed)
    inst._active_sessions = {}
    inst._inflight_dispatch_event_ids = {}
    return inst


def _session_key_by_chat(source, **kw):
    return f"sk:{source.chat_id}"


def _msg_event(event_id="ev-1", message_id="trigger-1", chat_id="chat-1", kind="message"):
    return SimpleNamespace(
        source=SimpleNamespace(chat_id=chat_id, thread_id=None),
        message_id=message_id,
        raw_message={"_grix_kind": kind, "event_id": event_id},
    )


def _with_ctx(client, coro):
    async def _run():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            return await coro
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
    return asyncio.run(_run())


def _register(inst, session_key, *event_ids):
    """模拟 _handle_message_packet 的派发登记。"""
    open_ids = inst._owner_states[""].session_open_event_ids.setdefault(session_key, [])
    for eid in event_ids:
        if eid not in open_ids:
            open_ids.append(eid)


def _run_turn(inst, client, event, outcome=None):
    """跑一轮 start→complete 生命周期。"""
    outcome = outcome if outcome is not None else adapter_mod.ProcessingOutcome.SUCCESS

    async def _turn():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, outcome)
        await inst.flush_deferred_failure_reports()

    _with_ctx(client, _turn())


# ── 1. 收口发生在任务完成时，且带真实结果 ────────────────────────────────────


def test_event_completes_at_processing_end(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # 任务开始时不发任何 event_result
    assert client.completed == []
    # 触发事件已被本轮认领
    assert inst._owner_states[""].session_running_event_ids["sk:chat-1"] == ["ev-1"]
    assert "sk:chat-1" not in inst._owner_states[""].session_open_event_ids

    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))
    assert client.completed == [{"event_id": "ev-1", "status": "responded", "message": None}]
    assert "sk:chat-1" not in inst._owner_states[""].session_running_event_ids


def test_failure_outcome_reports_failed(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")

    _run_turn(inst, client, _msg_event(), outcome=object())  # 非 SUCCESS 非 CANCELLED

    assert client.completed == [
        {"event_id": "ev-1", "status": "failed", "message": "message processing failed: Hermes finished without producing a reply"}
    ]


def test_failure_outcome_carries_gateway_error_detail(monkeypatch):
    """网关异常兜底文案是异常详情唯一能到适配器的通道，failed 结果要带上它。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")
    event = _msg_event()

    async def _turn():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, object())
        # 与网关顺序一致：钩子返回后才发错误文案
        assert client.completed == []
        inst._remember_failure_hint_from_reply(
            event.source.chat_id,
            "Sorry, I encountered an error (RuntimeError).\nprovider returned 401\nTry again or use /reset to start a fresh session.",
        )
        await inst.flush_deferred_failure_reports()

    _with_ctx(client, _turn())

    assert client.completed == [
        {"event_id": "ev-1", "status": "failed", "message": "RuntimeError: provider returned 401"}
    ]
    # 线索是一次性的，下一轮失败不会重复带上旧原因
    assert inst._owner_states[""].last_failure_hints == {}


def test_cancelled_outcome_reports_canceled(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")

    _run_turn(inst, client, _msg_event(), outcome=adapter_mod.ProcessingOutcome.CANCELLED)

    assert client.completed == [
        {"event_id": "ev-1", "status": "canceled", "message": "stopped by user"}
    ]


# ── 2. 排队消费时归属移交（pending pop）────────────────────────────────────


def test_pending_pop_transfers_ownership_to_next_run(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    # ev-2 / ev-3 排队且文本被合并（合并对象只剩 ev-2 的 raw），二者都已登记
    _register(inst, "sk:chat-1", "ev-2", "ev-3")
    inst._pending_messages["sk:chat-1"] = SimpleNamespace(message_id="m2")

    # 框架轮末 drain：pop 触发归属移交（无运行中轮次 → 归下一轮）
    _with_ctx(client, _pop_pending(inst, "sk:chat-1"))
    assert inst._owner_states[""].session_next_run_event_ids["sk:chat-1"] == ["ev-2", "ev-3"]
    assert "sk:chat-1" not in inst._owner_states[""].session_open_event_ids

    # 下一轮（合并事件，raw=ev-2）认领并收口两者
    _run_turn(inst, client, _msg_event(event_id="ev-2", message_id="m2"))
    assert sorted(c["event_id"] for c in client.completed) == ["ev-2", "ev-3"]
    assert all(c["status"] == "responded" for c in client.completed)


async def _pop_pending(inst, session_key):
    inst._pending_messages.pop(session_key, None)


def test_pending_pop_during_active_run_joins_running(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))

    # 运行期间 ev-2 排队，随后被 runner 中途注入当前轮（pending pop）
    _register(inst, "sk:chat-1", "ev-2")
    inst._pending_messages["sk:chat-1"] = SimpleNamespace(message_id="m2")
    _with_ctx(client, _pop_pending(inst, "sk:chat-1"))
    assert inst._owner_states[""].session_running_event_ids["sk:chat-1"] == ["ev-1", "ev-2"]

    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))
    assert sorted(c["event_id"] for c in client.completed) == ["ev-1", "ev-2"]


# ── 3. 认领不抓取未来轮次的事件 ──────────────────────────────────────────────


def test_claim_excludes_queued_future_events(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    # ev-1 属于本轮；ev-9 刚入队（pending 存在），属于未来轮次
    _register(inst, "sk:chat-1", "ev-1", "ev-9")
    inst._pending_messages["sk:chat-1"] = SimpleNamespace(message_id="m9")

    _run_turn(inst, client, _msg_event())

    # 本轮只收口 ev-1；ev-9 仍登记在册（pending 存在时也不做扫尾）
    assert [c["event_id"] for c in client.completed] == ["ev-1"]
    assert inst._owner_states[""].session_open_event_ids["sk:chat-1"] == ["ev-9"]


# ── 4. card_action 事件同样走任务收口 ────────────────────────────────────────


def test_card_action_event_completes_at_processing_end(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-ca")

    _run_turn(inst, client, _msg_event(event_id="ev-ca", kind="card_action"))

    assert [c["event_id"] for c in client.completed] == ["ev-ca"]


# ── 5. 撤回的触发消息上报 canceled/revoked ───────────────────────────────────


def test_revoked_trigger_reports_canceled(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")
    inst._owner_states[""].revoked_message_keys.add(("sk:chat-1", "trigger-1"))

    _run_turn(inst, client, _msg_event())

    assert client.completed == [
        {"event_id": "ev-1", "status": "canceled", "message": "revoked"}
    ]


# ── 6. 旁路消化的遗留事件按自身结果收口；inflight 不被误收 ───────────────────


def test_leftover_inline_events_swept_as_responded(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # 运行期间到达并被旁路消化的事件（如 clarify 文本答复）
    _register(inst, "sk:chat-1", "ev-inline")
    # 本轮失败：旁路事件自身被成功消化，仍应报 responded 而非跟随失败
    async def _complete():
        await inst.on_processing_complete(event, object())
        await inst.flush_deferred_failure_reports()

    _with_ctx(client, _complete())

    rows = {c["event_id"]: c["status"] for c in client.completed}
    assert rows == {"ev-1": "failed", "ev-inline": "responded"}


def test_inflight_event_not_swept(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _register(inst, "sk:chat-1", "ev-1")

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # ev-new 正在 handle_message 派发途中（可能马上入队）
    _register(inst, "sk:chat-1", "ev-new")
    inst._inflight_dispatch_event_ids["sk:chat-1"] = {"ev-new"}
    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))

    assert [c["event_id"] for c in client.completed] == ["ev-1"]
    # ev-new 留在登记表，等自己的派发链路归属
    assert inst._owner_states[""].session_open_event_ids["sk:chat-1"] == ["ev-new"]


def test_inflight_event_not_transferred_on_pending_pop(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    # ev-new 正在派发途中；此刻框架 drain 消费了 pending（属于更早的排队消息）
    _register(inst, "sk:chat-1", "ev-old", "ev-new")
    inst._inflight_dispatch_event_ids["sk:chat-1"] = {"ev-new"}
    inst._pending_messages["sk:chat-1"] = SimpleNamespace(message_id="m-old")

    _with_ctx(client, _pop_pending(inst, "sk:chat-1"))

    assert inst._owner_states[""].session_next_run_event_ids["sk:chat-1"] == ["ev-old"]
    assert inst._owner_states[""].session_open_event_ids["sk:chat-1"] == ["ev-new"]
