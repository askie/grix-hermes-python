"""Unit tests for quoted/context rendering prepended to the Hermes agent prompt."""

import json

from grix_hermes.adapter import _render_grix_context_block
from grix_hermes.protocol import GrixInboundMessage


def _msg(raw, session_type, message_id="999"):
    return GrixInboundMessage(
        event_id="e",
        session_id="s",
        sender_id="1",
        sender_name="n",
        chat_type="group",
        text="hi",
        message_id=message_id,
        session_type=session_type,
        raw=raw,
    )


def test_group_quoted_includes_sender():
    m = _msg({"context_messages": [{"msg_id": "100", "sender_id": "456", "content": "[引用消息]\n原文"}]}, 2)
    assert _render_grix_context_block(m) == "[引用消息] (来自 456)：原文"


def test_private_quoted_omits_sender():
    m = _msg({"context_messages": [{"msg_id": "100", "sender_id": "456", "content": "[引用消息]\n原文"}]}, 1)
    assert _render_grix_context_block(m) == "[引用消息]：原文"


def test_skips_current_message():
    m = _msg({"context_messages": [{"msg_id": "999", "sender_id": "456", "content": "当前"}]}, 2)
    assert _render_grix_context_block(m) == ""


def test_json_string_form():
    m = _msg(
        {"context_messages": json.dumps([{"msg_id": "100", "sender_id": "456", "content": "[引用消息]\n图"}])},
        2,
    )
    assert _render_grix_context_block(m) == "[引用消息] (来自 456)：图"


def test_group_plain_context_includes_sender():
    m = _msg({"context_messages": [{"msg_id": "50", "sender_id": "789", "content": "背景一句"}]}, 2)
    assert _render_grix_context_block(m) == "[789]：背景一句"


def test_empty_returns_blank():
    assert _render_grix_context_block(_msg({}, 2)) == ""
