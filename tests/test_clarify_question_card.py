"""Unit tests for the clarify → agent_question card override.

grix-hermes previously let the base hermes-agent adapter render clarify
prompts as a plain numbered text list — the server never recognized these as
a declared question, so waiting_question state / notifications never fired
and the client showed no tappable options. This overrides ``send_clarify``
to emit a standard ``agent_question`` biz_card instead, matching the server's
contract (see aibot/backend/internal/agentadapter/agentcards/normalize.go).
"""

import json
import sys
import types
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

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

    if "tools.clarify_gateway" not in sys.modules:
        cg = types.ModuleType("tools.clarify_gateway")
        cg.marked = []
        cg.mark_awaiting_text = lambda clarify_id: cg.marked.append(clarify_id)
        sys.modules["tools.clarify_gateway"] = cg

    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw_cfg = types.ModuleType("gateway.config")

        class _Platform:
            def __init__(self, name):
                self.value = name

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

from grix_hermes.card_links import build_agent_question_card  # noqa: E402
from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


class _SendResult:
    def __init__(self, success=False, message_id=None, error=None, raw_response=None, retryable=False):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.raw_response = raw_response
        self.retryable = retryable


adapter_mod.SendResult = _SendResult


def _decode_card(link: str):
    assert link.startswith("[")
    uri = link[link.index("](") + 2 : -1]
    parsed = urllib.parse.urlparse(uri)
    assert parsed.scheme == "grix"
    assert parsed.netloc + parsed.path == "card/agent_question"
    query = urllib.parse.parse_qs(parsed.query)
    return json.loads(query["d"][0])


# ── card_links.build_agent_question_card ────────────────────────────────────


def test_build_agent_question_card_shape():
    link = build_agent_question_card("clarify-1", "你想练哪个学段的题？", options=["A-Level", "IB", "IGCSE", "AP"])
    payload = _decode_card(link)

    assert payload["request_id"] == "clarify-1"
    assert payload["mode"] == "form"
    [q] = payload["questions"]
    assert q["index"] == 1
    assert q["header"] == "你想练哪个学段的题？"
    assert q["prompt"] == "你想练哪个学段的题？"
    assert q["options"] == ["A-Level", "IB", "IGCSE", "AP"]


def test_build_agent_question_card_no_options():
    link = build_agent_question_card("clarify-2", "开放式问题？")
    payload = _decode_card(link)
    [q] = payload["questions"]
    assert "options" not in q


def test_build_agent_question_card_requires_request_id():
    with pytest.raises(ValueError):
        build_agent_question_card("", "问题")


def test_build_agent_question_card_requires_question():
    with pytest.raises(ValueError):
        build_agent_question_card("clarify-3", "")


# ── adapter.send_clarify override ───────────────────────────────────────────


class FakeTransportClient:
    def __init__(self):
        self._config = SimpleNamespace(shared_owner_id=None)
        self.status = {"connected": True, "authed": True}
        self.sent = []

    async def send_text(self, session_id, content, *, reply_to_message_id=None,
                        thread_id=None, biz_card=None, channel_data=None, **kw):
        self.sent.append({"session_id": session_id, "content": content})
        return {"ok": True, "message_id": "m1"}


def _make_adapter(client):
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.config = SimpleNamespace(extra={})
    inst.connection = GrixConnectionConfig(endpoint="ws://x", agent_id="100", api_key="k")
    from collections import defaultdict
    import asyncio
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
    inst.truncate_message = lambda content, limit, len_fn=None: [content]
    return inst


async def _resolve_target(client, connection, chat_id, thread_id=None, source_hint=None):
    return str(chat_id), None


def _with_ctx(client, coro):
    import asyncio

    async def _run():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            return await coro
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
    return asyncio.run(_run())


def test_send_clarify_with_choices_sends_question_card(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    sys.modules["tools.clarify_gateway"].marked.clear()
    client = FakeTransportClient()
    inst = _make_adapter(client)

    result = _with_ctx(
        client,
        inst.send_clarify(
            chat_id="chat-1",
            question="你想练哪个学段的题？",
            choices=["A-Level", "IB", "IGCSE", "AP"],
            clarify_id="clarify-9",
            session_key="sk:chat-1",
        ),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert "grix://card/agent_question" in client.sent[0]["content"]
    payload = _decode_card(client.sent[0]["content"])
    assert payload["request_id"] == "clarify-9"
    assert payload["questions"][0]["options"] == ["A-Level", "IB", "IGCSE", "AP"]
    # Same text-intercept contract as the base implementation: choice-based
    # clarifies must flip into text-capture mode so a tapped option (which
    # round-trips as plain text via card_action) resolves the pending clarify.
    assert "clarify-9" in sys.modules["tools.clarify_gateway"].marked


def test_send_clarify_open_ended_sends_plain_text(monkeypatch):
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    sys.modules["tools.clarify_gateway"].marked.clear()
    client = FakeTransportClient()
    inst = _make_adapter(client)

    result = _with_ctx(
        client,
        inst.send_clarify(
            chat_id="chat-1",
            question="随便说说你的想法？",
            choices=None,
            clarify_id="clarify-10",
            session_key="sk:chat-1",
        ),
    )

    assert result.success is True
    assert "grix://card/" not in client.sent[0]["content"]
    assert client.sent[0]["content"] == "❓ 随便说说你的想法？"
    # Open-ended clarifies don't need text-capture mode flipped explicitly —
    # register() already defaults awaiting_text=True when choices is empty.
    assert sys.modules["tools.clarify_gateway"].marked == []
