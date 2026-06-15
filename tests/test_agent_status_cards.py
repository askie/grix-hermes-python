"""Unit tests for gateway status detection and thinking-card channel_data."""

from grix_hermes.agent_status_cards import (
    build_agent_status_channel_data,
    detect_agent_status,
)
from grix_hermes.tool_progress_cards import detect_tool_progress


# --- detection: real gateway status notifications --------------------------

def test_still_working_single_line():
    text = "⏳ Still working... (3 min elapsed)"
    assert detect_agent_status(text) == text


def test_still_working_with_iteration_detail():
    text = "⏳ Still working... (9 min elapsed — iteration 16/90, running: terminal)"
    assert detect_agent_status(text) == text


def test_no_activity_warning():
    text = (
        "⚠️ No activity for 15 min. If the agent does not respond soon, "
        "it will be timed out in 15 min. You can continue waiting or use /reset."
    )
    assert detect_agent_status(text) == text


def test_queued_for_next_turn():
    assert detect_agent_status("⏳ Queued for the next turn (iteration 4/8). ") is not None


def test_gateway_busy():
    assert detect_agent_status("⏳ Gateway is running and is not accepting another turn right now.") is not None


def test_agent_is_running_command_blocked():
    assert detect_agent_status("⏳ Agent is running — `/reset` can't run mid-turn.") is not None


def test_leading_and_trailing_whitespace_is_stripped():
    assert detect_agent_status("  \n⏳ Still working... (1 min elapsed)\n  ") == (
        "⏳ Still working... (1 min elapsed)"
    )


# --- detection: things that must NOT be classified as status ---------------

def test_normal_reply_is_not_status():
    assert detect_agent_status("Here is the summary you asked for.") is None


def test_empty_is_not_status():
    assert detect_agent_status("") is None
    assert detect_agent_status("   ") is None


def test_unrelated_emoji_prefix_is_not_status():
    # An hourglass that isn't one of the known gateway notices stays a normal
    # message (avoids hijacking genuine agent content).
    assert detect_agent_status("⏳ almost done with the migration") is None


def test_tool_progress_is_not_status():
    # Genuine tool progress must fall through to the tool_execution path.
    assert detect_agent_status('🔧 Edit: "src/app.py"') is None


# --- no conflict with tool-progress, and status is checked first -----------

def test_status_lines_do_not_match_tool_progress_today():
    # The two classifiers don't collide on current gateway strings, so routing
    # is unambiguous.  send() still checks status first to stay independent of
    # any future change to the tool-progress regex.
    for text in (
        "⏳ Still working... (2 min elapsed)",
        "⏳ Queued for the next turn (iteration 4/8). ",
        "⚠️ No activity for 15 min. Use /reset.",
    ):
        assert detect_tool_progress(text) is None
        assert detect_agent_status(text) is not None


# --- channel_data shape ----------------------------------------------------

def test_build_channel_data_shape():
    cd = build_agent_status_channel_data("⏳ Still working... (3 min elapsed)")
    assert cd == {
        "grix": {
            "thinking": {
                "content": "⏳ Still working... (3 min elapsed)",
            },
        },
    }
