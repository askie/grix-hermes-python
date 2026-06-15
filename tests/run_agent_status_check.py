"""Standalone runner for agent-status detection (pytest-free).

Mirrors tests/run_approval_local_action_check.py so it can be run directly
from the repo root without pytest collection picking up the package-root
__init__.py:

    python tests/run_agent_status_check.py
"""

from grix_hermes.agent_status_cards import (
    build_agent_status_channel_data,
    detect_agent_status,
)
from grix_hermes.tool_progress_cards import detect_tool_progress


STATUS_SAMPLES = [
    "⏳ Still working... (9 min elapsed — iteration 16/90, running: terminal)",
    "⏳ Still working... (3 min elapsed)",
    (
        "⚠️ No activity for 15 min. If the agent does not respond soon, it will "
        "be timed out in 15 min. You can continue waiting or use /reset."
    ),
    "⏳ Queued for the next turn (iteration 4/8). ",
    "⏳ Gateway is running and is not accepting another turn right now.",
    "⏳ Agent is running — `/reset` can't run mid-turn.",
]

NON_STATUS_SAMPLES = [
    "Here is the summary you asked for.",
    "",
    "   ",
    "⏳ almost done with the migration",   # hourglass but not a known notice
    '🔧 Edit: "src/app.py"',               # genuine tool progress
]


def main():
    # Every gateway status line is detected and returns the stripped text.
    for text in STATUS_SAMPLES:
        assert detect_agent_status(text) == text.strip(), text
        # No collision with tool-progress on current strings.
        assert detect_tool_progress(text) is None, text

    # Normal content is never classified as status.
    for text in NON_STATUS_SAMPLES:
        assert detect_agent_status(text) is None, repr(text)

    # Genuine tool progress still routes to the tool_execution path.
    assert detect_tool_progress('🔧 Edit: "src/app.py"') is not None

    # Whitespace is stripped; multi-line keeps the body.
    assert detect_agent_status("  \n⏳ Still working... (1 min)\n  ") == (
        "⏳ Still working... (1 min)"
    )

    # channel_data reuses the existing thinking card type.
    assert build_agent_status_channel_data("x") == {
        "grix": {"thinking": {"content": "x"}}
    }

    print("ok")


if __name__ == "__main__":
    main()
