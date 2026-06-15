"""Integration check: GrixAdapter.send() injects the right channel_data.

Verifies the wiring (not just the pure detector): a gateway status line is
tagged as a thinking card, genuine tool progress still becomes a tool_execution
card, and normal content carries no channel_data.

Requires the hermes core `gateway` package on the path (same dependency as
tests/run_approval_local_action_check.py).  Run from the repo root:

    PYTHONPATH=.:/path/to/hermes-agent python tests/run_agent_status_send_check.py
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from grix_hermes.adapter import GrixAdapter


def _adapter(client):
    a = object.__new__(GrixAdapter)
    a._get_ready_client = AsyncMock(return_value=client)
    a._enforce_send_rate = AsyncMock()
    a._latest_sources = {}
    a.connection = None
    a._metadata_thread_id = lambda metadata: None
    a._tool_progress_msg_ids = set()
    a._pending_messages = {}
    a._busy_ack_msg_ids = {}
    # Isolate message chunking so we only assert on channel_data routing.
    a.MAX_MESSAGE_LENGTH = 100_000
    a.format_message = lambda content: content
    a.truncate_message = lambda text, maxlen, len_fn=None: [text]
    a._message_size = lambda s: len(s)
    return a


def _send(content):
    client = SimpleNamespace(
        send_text=AsyncMock(return_value={"ok": True, "message_id": "m1"})
    )
    adapter = _adapter(client)
    with patch(
        "grix_hermes.adapter.resolve_grix_target",
        AsyncMock(return_value=("sess-1", "thread-1")),
    ):
        result = asyncio.run(adapter.send("chat-1", content))
    assert result.success, result
    assert client.send_text.await_count == 1
    return client.send_text.await_args.kwargs.get("channel_data")


def main():
    status = "⏳ Still working... (9 min elapsed — iteration 16/90, running: terminal)"
    assert _send(status) == {"grix": {"thinking": {"content": status}}}

    # Genuine tool progress still routes to the tool_execution card.
    cd_tool = _send('🔧 Edit: "src/app.py"')
    assert cd_tool == {"grix": {"toolExecution": {"summary_text": 'Edit: src/app.py'}}}, cd_tool

    # Normal content carries no channel_data.
    assert _send("Here is the summary you asked for.") is None

    print("ok")


if __name__ == "__main__":
    main()
