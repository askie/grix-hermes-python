"""事件收口时机单元测试。

对齐 connector 语义：event_result 必须在任务真正结束时按真实结果发出，
不能在消息刚派发（handle_message 返回）时就报 responded。

覆盖：
1. on_processing_start 认领登记的事件，on_processing_complete 按结果统一收口；
2. 被合并进同一轮的排队事件一起收口；
3. 失败结果发 failed；
4. 撤回的触发消息不上报结果，同轮其他事件正常收口；
5. 旁路消化的遗留事件在队列空时随轮收口，派发途中(inflight)的事件不被误收。

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
        gw_platforms_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": object()})
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

if not hasattr(adapter_mod.ProcessingOutcome, "SUCCESS"):
    adapter_mod.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": object()})


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
    inst._pending_messages = {}
    inst._active_sessions = {}
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


def _run_turn(inst, client, event, outcome=None):
    """跑一轮 start→complete 生命周期。"""
    outcome = outcome if outcome is not None else adapter_mod.ProcessingOutcome.SUCCESS

    async def _turn():
        await inst.on_processing_start(event)
        await inst.on_processing_complete(event, outcome)

    _with_ctx(client, _turn())


# ── 1. 收口发生在任务完成时，且带真实结果 ────────────────────────────────────


def test_event_completes_at_processing_end(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-1"]

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # 任务开始时不发任何 event_result
    assert client.completed == []
    # 登记已被认领
    assert "sk:chat-1" not in inst._owner_states[""].session_open_event_ids
    assert inst._owner_states[""].session_running_event_ids["sk:chat-1"] == ["ev-1"]

    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))
    assert client.completed == [{"event_id": "ev-1", "status": "responded", "message": None}]
    assert "sk:chat-1" not in inst._owner_states[""].session_running_event_ids


def test_failure_outcome_reports_failed(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-1"]

    _run_turn(inst, client, _msg_event(), outcome=object())  # 非 SUCCESS

    assert len(client.completed) == 1
    assert client.completed[0]["event_id"] == "ev-1"
    assert client.completed[0]["status"] == "failed"


# ── 2. 合并进同一轮的排队事件统一收口 ────────────────────────────────────────


def test_merged_queued_events_complete_together(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    # 模拟 ev-2 / ev-3 在上一轮运行期间排队且文本被合并（event_id 只剩 ev-2）
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-2", "ev-3"]

    _run_turn(inst, client, _msg_event(event_id="ev-2", message_id="m2"))

    assert sorted(c["event_id"] for c in client.completed) == ["ev-2", "ev-3"]
    assert all(c["status"] == "responded" for c in client.completed)


# ── 3. card_action 事件同样走任务收口 ────────────────────────────────────────


def test_card_action_event_completes_at_processing_end(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-ca"]

    _run_turn(inst, client, _msg_event(event_id="ev-ca", kind="card_action"))

    assert [c["event_id"] for c in client.completed] == ["ev-ca"]


# ── 4. 撤回的触发消息不上报结果，同轮其他事件正常收口 ────────────────────────


def test_revoked_trigger_skipped_but_others_complete(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-1", "ev-9"]
    inst._owner_states[""].revoked_message_keys.add(("sk:chat-1", "trigger-1"))

    _run_turn(inst, client, _msg_event())

    assert [c["event_id"] for c in client.completed] == ["ev-9"]


# ── 5. 旁路消化的遗留事件随轮收口；inflight 事件不被误收 ─────────────────────


def test_leftover_inline_events_swept_at_turn_end(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-1"]

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # 运行期间到达并被旁路消化的事件（如 clarify 文本答复）
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-inline"]
    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))

    assert sorted(c["event_id"] for c in client.completed) == ["ev-1", "ev-inline"]


def test_inflight_event_not_swept(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-1"]

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # ev-new 正在 handle_message 派发途中（可能马上入队）
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-new"]
    inst._inflight_dispatch()["sk:chat-1"] = {"ev-new"}
    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))

    assert [c["event_id"] for c in client.completed] == ["ev-1"]
    # ev-new 留在登记表，等下一轮认领
    assert inst._owner_states[""].session_open_event_ids["sk:chat-1"] == ["ev-new"]


def test_queued_events_not_swept_when_pending_exists(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-1"]

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))
    # ev-2 已排队等待下一轮（pending 存在时不做扫尾）
    inst._owner_states[""].session_open_event_ids["sk:chat-1"] = ["ev-2"]
    inst._pending_messages["sk:chat-1"] = SimpleNamespace(message_id="m2")
    _with_ctx(client, inst.on_processing_complete(event, adapter_mod.ProcessingOutcome.SUCCESS))

    assert [c["event_id"] for c in client.completed] == ["ev-1"]
    assert inst._owner_states[""].session_open_event_ids["sk:chat-1"] == ["ev-2"]
