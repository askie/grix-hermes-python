"""Tests for the /grix question command interception (card-tap answer recovery).

Discovered live (sessions 9834f226… and b0435d03…, 2026-07-05): tapping an
agent_question card option produced a ``grix://card/agent_question_reply``
URI that the server rewrote to ``/grix question <request_id> <value>`` before
delivery (hermes strict profile). hermes-agent swallowed it as an unknown
slash command, the pending clarify waited the full 600s timeout, and the
model continued with a self-picked default. These tests pin the fix: the
adapter must resolve the pending clarify by request_id, and when nothing is
pending (timeout already fired / stale card) the readable answer text must
flow on as a normal message instead of an unknown command.
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

    # Other test modules may have installed a bare clarify_gateway stub
    # first (module-level stubs are shared across the whole pytest run) —
    # augment whatever is there instead of assuming we run first.
    cg = sys.modules.get("tools.clarify_gateway")
    if cg is None:
        cg = types.ModuleType("tools.clarify_gateway")
        cg.marked = []
        cg.mark_awaiting_text = lambda clarify_id: cg.marked.append(clarify_id)
        sys.modules["tools.clarify_gateway"] = cg
    cg.resolve_calls = []
    cg.resolve_result = False
    cg.resolve_raises = False

    def _resolve(clarify_id, response):
        if cg.resolve_raises:
            raise RuntimeError("boom")
        cg.resolve_calls.append((clarify_id, response))
        return cg.resolve_result

    cg.resolve_gateway_clarify = _resolve

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

from grix_hermes.question_command import parse_grix_question_command  # noqa: E402
from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.protocol import GrixInboundMessage  # noqa: E402


# ── parse_grix_question_command ──────────────────────────────────────────────


def test_parse_single_value():
    # Verbatim shape from the second live incident (session b0435d03…).
    assert parse_grix_question_command("/grix question 5221aa7b38 Edexcel") == (
        "5221aa7b38",
        "Edexcel",
    )


def test_parse_single_value_with_spaces_and_unicode():
    # Verbatim shape from the first live incident (session 9834f226…):
    # the answer keeps embedded spaces, em-dash and parentheses intact.
    parsed = parse_grix_question_command(
        "/grix question fd2f64c2f9 Edexcel — 力学 (Mechanics)"
    )
    assert parsed == ("fd2f64c2f9", "Edexcel — 力学 (Mechanics)")


def test_parse_map_response():
    # grixactions.LegacyQuestionCommand map form: "k=v; k2=v2".
    parsed = parse_grix_question_command("/grix question req-1 board=Edexcel; topic=力学")
    assert parsed == ("req-1", "board=Edexcel; topic=力学")


def test_parse_accept_cancel_tokens():
    assert parse_grix_question_command("/grix question req-1 __grix_accept__") == ("req-1", "accept")
    assert parse_grix_question_command("/grix question req-1 __grix_cancel__") == ("req-1", "cancel")


def test_parse_rejects_non_question_input():
    assert parse_grix_question_command("普通消息 question 里带这个词") is None
    assert parse_grix_question_command("/grix open /some/path") is None
    assert parse_grix_question_command("/grix questionnaire req-1 x") is None
    assert parse_grix_question_command("/grix question req-only-no-answer") is None
    assert parse_grix_question_command("/grix question") is None
    assert parse_grix_question_command("") is None
    assert parse_grix_question_command(None) is None


def test_parse_tolerates_surrounding_whitespace():
    assert parse_grix_question_command("  /grix question req-1   answer  ") == ("req-1", "answer")


# ── adapter._try_resolve_question_reply ──────────────────────────────────────


def _make_message(text: str) -> GrixInboundMessage:
    return GrixInboundMessage(
        event_id="evt-1",
        session_id="sess-1",
        sender_id="u1",
        sender_name="user",
        chat_type="dm",
        text=text,
        message_id="m1",
        raw={},
    )


def _make_adapter():
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst._client = None  # completion path exercised separately
    inst.completed = []

    async def _complete(event_id, status=None, message=None):
        inst.completed.append((event_id, status))

    inst._complete_event_if_needed = _complete
    return inst


def _cg():
    cg = sys.modules["tools.clarify_gateway"]
    cg.resolve_calls.clear()
    cg.resolve_result = False
    cg.resolve_raises = False
    return cg


def test_pending_clarify_resolved_swallows_message():
    # The discovered scenario, fixed: a tapped option arrives while clarify
    # blocks — it must resolve the exact request_id and not become a model turn.
    cg = _cg()
    cg.resolve_result = True
    inst = _make_adapter()
    msg = _make_message("/grix question fd2f64c2f9 Edexcel — 力学 (Mechanics)")

    handled, out = asyncio.run(inst._try_resolve_question_reply(msg, "sk-1"))

    assert handled is True
    assert cg.resolve_calls == [("fd2f64c2f9", "Edexcel — 力学 (Mechanics)")]


def test_resolved_completes_event_when_client_present():
    cg = _cg()
    cg.resolve_result = True
    inst = _make_adapter()
    inst._client = object()
    msg = _make_message("/grix question req-9 CAIE")

    handled, _ = asyncio.run(inst._try_resolve_question_reply(msg, "sk-1"))

    assert handled is True
    assert inst.completed == [("evt-1", adapter_mod.STATUS_RESPONDED)]


def test_no_pending_clarify_falls_through_as_readable_text():
    # The discovered scenario's tail: clarify already timed out (600s) —
    # the answer must reach the model as plain text, not an unknown command.
    cg = _cg()
    cg.resolve_result = False
    inst = _make_adapter()
    msg = _make_message("/grix question 5221aa7b38 Edexcel")

    handled, out = asyncio.run(inst._try_resolve_question_reply(msg, "sk-1"))

    assert handled is False
    assert out.text == "Edexcel"
    assert not out.text.startswith("/")
    assert cg.resolve_calls == [("5221aa7b38", "Edexcel")]


def test_resolve_exception_still_falls_through():
    cg = _cg()
    cg.resolve_raises = True
    inst = _make_adapter()
    msg = _make_message("/grix question req-1 Edexcel")

    handled, out = asyncio.run(inst._try_resolve_question_reply(msg, "sk-1"))

    assert handled is False
    assert out.text == "Edexcel"


def test_plain_text_untouched():
    cg = _cg()
    inst = _make_adapter()
    msg = _make_message("A level 物理")

    handled, out = asyncio.run(inst._try_resolve_question_reply(msg, "sk-1"))

    assert handled is False
    assert out is msg
    assert cg.resolve_calls == []
