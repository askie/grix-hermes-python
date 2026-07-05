"""最终应答引用收敛单元测试。

覆盖三块行为（对齐 connector 的 grix_reply 语义）：
1. adapter.send 默认不再把 reply_to 透传为引用（过程消息零引用、零误触发）；
   仅 force_quote=True（grix_reply 工具路径）时首个分片带引用。
2. edit_message 对瞬时失败（ws 内部重连窗口）就地重试，不再把失败上抛给
   hermes 网关（避免整轮编辑被永久禁用、内容碎片化）。
3. grix_reply 工具：解析进行中任务的应答目标，自动补引用发送最终总结。

走 stub 模式（同 test_agent_share.py），不依赖 hermes-agent host。
"""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── stub host modules（与 test_agent_share 同模式；若已装则按需补强属性） ──
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
from grix_hermes import reply_tool as reply_tool_mod  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


# 可用的 SendResult（test_agent_share 的 stub 版本不可构造，这里直接换掉
# adapter 模块内绑定的名字，避免受测试文件加载顺序影响）。
class _SendResult:
    def __init__(self, success=False, message_id=None, error=None, raw_response=None, retryable=False):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.raw_response = raw_response
        self.retryable = retryable


adapter_mod.SendResult = _SendResult

# test_agent_share 的 stub ProcessingOutcome 没有 SUCCESS 属性；补齐以免受
# 测试文件加载顺序影响。
if not hasattr(adapter_mod.ProcessingOutcome, "SUCCESS"):
    adapter_mod.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": object()})


class FakeTransportClient:
    """最小 transport 假件：记录 send_text / edit_message 调用。"""

    def __init__(self, shared_owner_id=None):
        self._config = SimpleNamespace(shared_owner_id=shared_owner_id)
        self.status = {"connected": True, "authed": True}
        self.sent = []
        self.edits = []
        self.edit_errors = []  # 每次 edit 依序抛出的异常；耗尽后成功

    async def send_text(self, session_id, content, *, reply_to_message_id=None,
                        thread_id=None, biz_card=None, channel_data=None, **kw):
        self.sent.append({
            "session_id": session_id,
            "content": content,
            "reply_to_message_id": reply_to_message_id,
        })
        return {"ok": True, "message_id": f"m{len(self.sent)}"}

    async def edit_message(self, session_id, message_id, text, **kw):
        self.edits.append({"session_id": session_id, "message_id": message_id, "text": text})
        if self.edit_errors:
            raise self.edit_errors.pop(0)
        return {"ok": True, "message_id": message_id}


def _make_adapter(client=None):
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.config = SimpleNamespace(extra={})
    inst.connection = GrixConnectionConfig(endpoint="ws://x", agent_id="100", api_key="k")
    from collections import defaultdict
    inst._owner_states = defaultdict(adapter_mod._OwnerState)
    inst._last_send_at = 0.0
    inst._send_lock = asyncio.Lock()
    inst._reconnect_lock = asyncio.Lock()
    inst._shared_clients = {}
    inst._share_sync_lock = asyncio.Lock()
    inst._shutting_down = False
    inst._disconnect_requested = False
    inst._client = client or FakeTransportClient()
    inst._pending_messages = {}
    inst.truncate_message = lambda content, limit, len_fn=None: [content]
    return inst


async def _resolve_target(client, connection, chat_id, thread_id=None, source_hint=None):
    return str(chat_id), None


def _with_ctx(client, coro):
    """在指定 client 的 packet ContextVar 作用域内运行协程。"""
    async def _run():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            return await coro
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
    return asyncio.run(_run())


# ── 1. send 引用收敛 ──────────────────────────────────────────────────────────


def test_send_suppresses_quote_by_default(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)

    result = _with_ctx(client, inst.send("chat-1", "process update", reply_to="trigger-1"))

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["reply_to_message_id"] is None


def test_send_force_quote_first_chunk_only(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    inst.truncate_message = lambda content, limit, len_fn=None: [content[:4], content[4:]]

    result = _with_ctx(
        client,
        inst.send("chat-1", "finalanswer", reply_to="trigger-1", force_quote=True),
    )

    assert result.success is True
    assert [s["reply_to_message_id"] for s in client.sent] == ["trigger-1", None]


# ── 2. edit_message 瞬时失败重试 ──────────────────────────────────────────────


def test_edit_message_retries_transient_failure(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    monkeypatch.setattr(adapter_mod.GrixAdapter, "_EDIT_RETRY_DELAY_S", 0.0)
    client = FakeTransportClient()
    client.edit_errors = [TimeoutError("edit_msg timeout")]
    inst = _make_adapter(client)

    result = _with_ctx(client, inst.edit_message("chat-1", "m1", "updated text"))

    assert result.success is True
    assert len(client.edits) == 2


def test_edit_message_gives_up_after_attempts(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    monkeypatch.setattr(adapter_mod.GrixAdapter, "_EDIT_RETRY_DELAY_S", 0.0)
    client = FakeTransportClient()
    client.edit_errors = [TimeoutError("edit_msg timeout")] * 10
    inst = _make_adapter(client)

    result = _with_ctx(client, inst.edit_message("chat-1", "m1", "updated text"))

    assert result.success is False
    assert result.retryable is True
    assert len(client.edits) == adapter_mod.GrixAdapter._EDIT_RETRY_ATTEMPTS


def test_edit_message_permanent_error_no_retry(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    monkeypatch.setattr(adapter_mod.GrixAdapter, "_EDIT_RETRY_DELAY_S", 0.0)
    client = FakeTransportClient()
    client.edit_errors = [ValueError("unauthorized")] * 10
    inst = _make_adapter(client)

    result = _with_ctx(client, inst.edit_message("chat-1", "m1", "updated text"))

    assert result.success is False
    assert result.retryable is False
    assert len(client.edits) == 1


# ── 3. 应答目标登记 / 清除 ────────────────────────────────────────────────────


def _msg_event(chat_id="chat-1", message_id="trigger-1", event_id="ev-1"):
    return SimpleNamespace(
        raw_message={"_grix_kind": "message", "event_id": event_id},
        message_id=message_id,
        source=SimpleNamespace(chat_id=chat_id),
    )


def _session_key_by_chat(source, **kw):
    return f"sk:{source.chat_id}"


def test_processing_lifecycle_tracks_reply_target(monkeypatch):
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_by_chat)
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _noop_complete(*a, **kw):
        return None

    inst._complete_event_if_needed = _noop_complete

    event = _msg_event()
    _with_ctx(client, inst.on_processing_start(event))

    targets = inst._owner_states[""].active_reply_targets
    assert "sk:chat-1" in targets
    entry = targets["sk:chat-1"]
    assert entry["chat_id"] == "chat-1"
    assert entry["message_id"] == "trigger-1"
    assert entry["event_id"] == "ev-1"
    assert entry["client"] is client

    _with_ctx(client, inst.on_processing_complete(event, True))
    assert "sk:chat-1" not in inst._owner_states[""].active_reply_targets


# ── 4. grix_reply 工具 ───────────────────────────────────────────────────────


def _install_runner(monkeypatch, inst):
    runner = SimpleNamespace(adapters=SimpleNamespace(get=lambda _p: inst))
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)


def _put_target(inst, chat_id="chat-1", message_id="trigger-1", client=None,
                session_key=None, started_at=0.0):
    key = session_key or f"sk:{chat_id}"
    inst._owner_states[""].active_reply_targets[key] = {
        "session_key": key,
        "chat_id": chat_id,
        "message_id": message_id,
        "event_id": "ev-1",
        "client": client,
        "loop": None,
        "started_at": started_at,
    }


def test_reply_tool_quotes_trigger_message(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, client=client)

    out = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "最终总结"}))

    assert out.startswith("OK:")
    assert len(client.sent) == 1
    assert client.sent[0]["reply_to_message_id"] == "trigger-1"
    assert client.sent[0]["content"] == "最终总结"


def test_reply_tool_no_active_target_errors(monkeypatch):
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)

    out = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "x"}))

    assert out.startswith("ERR:")
    assert "no active task" in out


def test_reply_tool_multiple_targets_requires_session_id(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, chat_id="chat-1", message_id="t1", client=client)
    _put_target(inst, chat_id="chat-2", message_id="t2", client=client)

    out = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "x"}))
    assert out.startswith("ERR:")
    assert "session_id" in out

    out = asyncio.run(
        reply_tool_mod._grix_reply_handler({"text": "结论", "session_id": "chat-2"})
    )
    assert out.startswith("OK:")
    assert client.sent[-1]["reply_to_message_id"] == "t2"
    assert client.sent[-1]["session_id"] == "chat-2"


def test_reply_tool_quote_override(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, client=client)

    out = asyncio.run(
        reply_tool_mod._grix_reply_handler(
            {"text": "结论", "quoted_message_id": "other-msg"}
        )
    )

    assert out.startswith("OK:")
    assert client.sent[0]["reply_to_message_id"] == "other-msg"


def test_reply_tool_context_key_disambiguates_same_chat(monkeypatch):
    """同一群两个 per-user session 并发时，按任务链路 context 的 session_key 精确归属。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, chat_id="group-1", message_id="from-user-a", client=client,
                session_key="sk:group-1:user-a", started_at=1.0)
    _put_target(inst, chat_id="group-1", message_id="from-user-b", client=client,
                session_key="sk:group-1:user-b", started_at=2.0)

    async def _run_in_ctx():
        token = adapter_mod._CURRENT_REPLY_SESSION_KEY.set("sk:group-1:user-a")
        try:
            return await reply_tool_mod._grix_reply_handler({"text": "A 的结论"})
        finally:
            adapter_mod._CURRENT_REPLY_SESSION_KEY.reset(token)

    out = asyncio.run(_run_in_ctx())

    assert out.startswith("OK:")
    assert client.sent[-1]["reply_to_message_id"] == "from-user-a"


def test_reply_tool_same_chat_without_context_picks_latest(monkeypatch):
    """拿不到 context 归属时，显式 session_id 在同群多任务下取最新启动的。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, chat_id="group-1", message_id="older", client=client,
                session_key="sk:group-1:user-a", started_at=1.0)
    _put_target(inst, chat_id="group-1", message_id="newer", client=client,
                session_key="sk:group-1:user-b", started_at=2.0)

    out = asyncio.run(
        reply_tool_mod._grix_reply_handler({"text": "结论", "session_id": "group-1"})
    )

    assert out.startswith("OK:")
    assert client.sent[-1]["reply_to_message_id"] == "newer"


def test_reply_tool_second_call_drops_quote(monkeypatch):
    """完成信号只能出现一次：重复调用不再带引用，避免二次触发接活。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, client=client)

    out1 = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "结论一"}))
    out2 = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "补充说明"}))

    assert out1.startswith("OK:") and out2.startswith("OK:")
    assert client.sent[0]["reply_to_message_id"] == "trigger-1"
    assert client.sent[1]["reply_to_message_id"] is None
    assert "already been sent" in out2 or "already" in out2


def test_reply_tool_failed_send_does_not_mark_replied(monkeypatch):
    """首次发送失败不应占用完成信号，重试仍要带引用。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, client=client)

    calls = {"n": 0}
    real_send_text = client.send_text

    async def _flaky_send_text(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("send_msg timeout")
        return await real_send_text(*a, **kw)

    client.send_text = _flaky_send_text

    out1 = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "结论"}))
    assert out1.startswith("ERR:")

    out2 = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "结论"}))
    assert out2.startswith("OK:")
    assert client.sent[-1]["reply_to_message_id"] == "trigger-1"


def test_edit_message_permanent_nack_no_retry(monkeypatch):
    """服务端 4xxx NACK（非 4008）重试不会变好，应立即上抛。"""
    from grix_hermes.transport import GrixPacketError

    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    monkeypatch.setattr(adapter_mod.GrixAdapter, "_EDIT_RETRY_DELAY_S", 0.0)
    client = FakeTransportClient()
    client.edit_errors = [GrixPacketError("send_nack", 4001, "message not found")] * 10
    inst = _make_adapter(client)

    result = _with_ctx(client, inst.edit_message("chat-1", "m1", "updated text"))

    assert result.success is False
    assert result.retryable is False
    assert len(client.edits) == 1


def test_reply_tool_falls_back_to_primary_client(monkeypatch):
    """目标里没记到来源 client（登记时脱离 packet 上下文）时回退主连接。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _install_runner(monkeypatch, inst)
    _put_target(inst, client=None)

    out = asyncio.run(reply_tool_mod._grix_reply_handler({"text": "结论"}))

    assert out.startswith("OK:")
    assert len(client.sent) == 1
    assert client.sent[0]["reply_to_message_id"] == "trigger-1"
