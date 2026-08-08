"""审计回放 adapter 钩子单元测试。

覆盖（对齐 connector 语义）：
1. 事件终态统一汇聚点 _complete_event_if_needed 触发审计 finalize，
   audit_state 按 finalizing → 终态（partial）回传，responded/canceled 映射为
   connector 回放 outcome（completed/cancelled）。
2. 输出采集只覆盖最终应答（is_final_reply / notify），过程消息不进回放正文。
3. 未审计事件的收口是空操作，不发 audit_state。

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
from grix_hermes.audit import HermesAuditStore  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


class _SendResult:
    def __init__(self, success=False, message_id=None, error=None, raw_response=None, retryable=False):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.raw_response = raw_response
        self.retryable = retryable


adapter_mod.SendResult = _SendResult

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
    def __init__(self, shared_owner_id=None):
        self._config = SimpleNamespace(shared_owner_id=shared_owner_id)
        self.status = {"connected": True, "authed": True}
        self.sent = []
        self.audit_states = []
        self.completed = []

    async def send_text(self, session_id, content, *, reply_to_message_id=None,
                        thread_id=None, biz_card=None, channel_data=None, **kw):
        self.sent.append({
            "session_id": session_id,
            "content": content,
            "reply_to_message_id": reply_to_message_id,
        })
        return {"ok": True, "message_id": f"m{len(self.sent)}"}

    async def send_audit_state(self, payload, **kw):
        self.audit_states.append(dict(payload))
        return {"ok": True}

    async def complete_event(self, event_id, status, message=None, **kw):
        self.completed.append({"event_id": event_id, "status": status, "message": message})
        return {"ok": True}


def _make_adapter(client, audit_root):
    from collections import defaultdict

    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.config = SimpleNamespace(extra={})
    inst.connection = GrixConnectionConfig(endpoint="ws://x", agent_id="100", api_key="k")
    inst._owner_states = defaultdict(adapter_mod._OwnerState)
    inst._last_send_at = 0.0
    inst._send_lock = asyncio.Lock()
    inst._reconnect_lock = asyncio.Lock()
    inst._shared_clients = {}
    inst._share_sync_lock = asyncio.Lock()
    inst._shutting_down = False
    inst._disconnect_requested = False
    inst._client = client
    inst._pending_messages = {}
    inst._active_sessions = {}
    inst._inflight_dispatch_event_ids = {}
    inst._audit_storage_root = str(audit_root)
    inst._audit_store = None
    inst.truncate_message = lambda content, limit, len_fn=None: [content]
    return inst


async def _resolve_target(client, connection, chat_id, thread_id=None, source_hint=None):
    return str(chat_id), thread_id


def _with_ctx(client, coro):
    async def _run():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            return await coro
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
    return asyncio.run(_run())


def _audit_event(chat_id="chat-1", message_id="12345", event_id="ev-1", text="查一下天气"):
    return SimpleNamespace(
        raw_message={
            "_grix_kind": "message",
            "event_id": event_id,
            "session_id": chat_id,
            "content": text,
            "extra": {"audit": {"enabled": True, "scope": "turn", "profile": "replay"}},
        },
        message_id=message_id,
        text=text,
        source=SimpleNamespace(chat_id=chat_id),
    )


def _begin(inst, client, event, session_key="sk:chat-1"):
    return _with_ctx(client, inst._begin_audit_turn(event, session_key))


def test_begin_turn_emits_accepted_then_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client, tmp_path / "audit")

    _begin(inst, client, _audit_event())

    states = [s["state"] for s in client.audit_states]
    assert states == ["accepted", "recording"]
    first = client.audit_states[0]
    assert first["event_id"] == "ev-1"
    assert first["session_id"] == "chat-1"
    assert first["msg_id"] == "12345"
    assert first["audit_id"].startswith("audit-")
    assert first["turn_id"].startswith("turn-")
    assert isinstance(first["updated_at"], int)


def test_complete_event_finalizes_turn_and_reports_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client, tmp_path / "audit")
    _begin(inst, client, _audit_event())
    client.audit_states.clear()

    _with_ctx(client, inst._complete_event_if_needed("ev-1", status="responded"))

    states = [s["state"] for s in client.audit_states]
    assert states == ["finalizing", "partial"]
    terminal = client.audit_states[-1]
    assert terminal["revision"] == 1
    assert terminal["quality"] == "partial"
    # 终态后审计轮已弹出，重复收口是空操作。
    _with_ctx(client, inst._complete_event_if_needed("ev-1", status="responded"))
    assert [s["state"] for s in client.audit_states] == ["finalizing", "partial"]

    store = HermesAuditStore(str(tmp_path / "audit"))
    manifest = store.action("audit_get_manifest", {
        "session_id": "chat-1",
        "audit_id": terminal["audit_id"],
        "turn_id": terminal["turn_id"],
    })
    assert manifest["status"] == "ok"
    result = manifest["result"]
    assert result["status"] == "completed"
    assert result["raw_api_capture"]["status"] == "not_requested"
    assert result["quality"]["raw_requests_status"] == "not_requested"
    spans = store.action("audit_list_spans", {
        "session_id": "chat-1",
        "audit_id": terminal["audit_id"],
        "turn_id": terminal["turn_id"],
    })["result"]["items"]
    assert spans[0]["status"] == "completed"
    assert spans[0]["started_at"].endswith("Z")
    assert spans[0]["provenance"] == {"source": "reconstructed", "accuracy": "estimated"}


def test_cancelled_completion_maps_to_cancelled_outcome(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client, tmp_path / "audit")
    _begin(inst, client, _audit_event())

    _with_ctx(client, inst._complete_event_if_needed("ev-1", status="canceled", message="stopped by user"))

    terminal = client.audit_states[-1]
    store = HermesAuditStore(str(tmp_path / "audit"))
    manifest = store.action("audit_get_manifest", {
        "session_id": "chat-1",
        "audit_id": terminal["audit_id"],
        "turn_id": terminal["turn_id"],
    })
    assert manifest["result"]["status"] == "cancelled"


def test_send_captures_only_final_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client, tmp_path / "audit")
    _begin(inst, client, _audit_event())

    async def _conversation():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        key_token = adapter_mod._CURRENT_REPLY_SESSION_KEY.set("sk:chat-1")
        try:
            await inst.send("chat-1", "⏳ Working — 1 min…")
            await inst.send("chat-1", "最终结论", is_final_reply=True)
            await inst._complete_event_if_needed("ev-1", status="responded")
        finally:
            adapter_mod._CURRENT_REPLY_SESSION_KEY.reset(key_token)
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)

    asyncio.run(_conversation())

    terminal = client.audit_states[-1]
    store = HermesAuditStore(str(tmp_path / "audit"))
    manifest = store.action("audit_get_manifest", {
        "session_id": "chat-1",
        "audit_id": terminal["audit_id"],
        "turn_id": terminal["turn_id"],
    })["result"]
    output_refs = [r for r in manifest["content_refs"] if r["kind"] == "final_response"]
    assert len(output_refs) == 1
    chunk = store.action("audit_get_content_chunk", {
        "session_id": "chat-1",
        "audit_id": terminal["audit_id"],
        "turn_id": terminal["turn_id"],
        "content_id": output_refs[0]["content_id"],
    })
    assert chunk["result"]["value"] == "最终结论"
    assert output_refs[0]["capture_level"] == "reconstructed"
    assert output_refs[0]["estimated_tokens"] >= 1


def test_unaudited_event_completion_is_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client, tmp_path / "audit")

    _with_ctx(client, inst._complete_event_if_needed("ev-plain", status="responded"))

    assert client.audit_states == []
    assert client.completed == [{"event_id": "ev-plain", "status": "responded", "message": None}]
    assert not (tmp_path / "audit").exists() or not list((tmp_path / "audit").rglob("*.json"))


def test_audit_state_msg_id_normalized_or_dropped(tmp_path):
    """后端 MsgID 是 json:",string" int64 且要求 >0：空串/非数字/"0" 一律省略。"""
    client = FakeTransportClient()
    inst = _make_adapter(client, tmp_path / "audit")

    async def _send(payload):
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            await inst._send_audit_state(payload)
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)

    asyncio.run(_send({"event_id": "e1", "session_id": "s1", "state": "accepted", "msg_id": "12345"}))
    asyncio.run(_send({"event_id": "e2", "session_id": "s1", "state": "accepted", "msg_id": ""}))
    asyncio.run(_send({"event_id": "e3", "session_id": "s1", "state": "accepted", "msg_id": "abc"}))
    asyncio.run(_send({"event_id": "e4", "session_id": "s1", "state": "accepted", "msg_id": "0"}))
    asyncio.run(_send({"event_id": "e5", "session_id": "s1", "state": "accepted"}))

    assert client.audit_states[0]["msg_id"] == "12345"
    for entry in client.audit_states[1:]:
        assert entry.get("msg_id") is None
