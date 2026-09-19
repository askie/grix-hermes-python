"""Agent status card support for the Grix adapter.

The Hermes gateway (core) emits periodic *system status* notifications while a
long-running turn is in flight — "still working", inactivity warnings, queued /
gateway-busy notices.  These are not agent replies; surfacing them as normal
message bubbles is noisy.  This module detects those status lines so the adapter
can tag them with ``channel_data.grix.thinking`` and let the backend/client
render them as a lightweight thinking card instead of a formal bubble.

Busy-ack / queue / steer / drain runtime notices (⚡ Interrupting, ⏳ Queued,
⏩ Steered, ↪ Redirected, ⏳ Gateway …) are a stricter subset: they must never
enter a Grix chat at all (especially under delegate, where they would appear as
the owner speaking).  Use :func:`detect_gateway_runtime_notice` to swallow them.

Source of the status strings (gateway/run.py / run_busy.py):
  - "⏳ Still working... (N min elapsed — iteration X/Y, running: tool)"
  - "⏳ Working — N min — iteration X/Y, tool"
  - "⚠️ No activity for N min. ..."
  - "⏳ Queued for the next turn ..."
  - "⏳ Gateway is running ..." / "⏳ Gateway running — queued ..."
  - "⏳ Agent is running — `/cmd` can't run ..."
  - "⚡ Interrupting current task ..."
  - "⏩ Steered into current run ..."
  - "↪ Redirected current run ..."
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Known gateway status / progress notifications.  Matched against the first
# line only, so a status line followed by extra context still classifies.
# Kept as an explicit whitelist of句式 rather than a broad "emoji prefix" rule
# to avoid misclassifying genuine agent replies that happen to open with ⏳/⚠️.
_STATUS_PATTERNS = (
    re.compile(r"^⏳\s+Still working\b"),
    re.compile(r"^⏳\s+Working\b"),
    re.compile(r"^⚠️\s+No activity for\b"),
    re.compile(r"^⏳\s+Queued for the next turn\b"),
    re.compile(r"^⏳\s+Gateway\b"),
    re.compile(r"^⏳\s+Agent is running\b"),
)

# Busy-path runtime notices from gateway/run_busy.py.  These are ack/queue/
# steer/drain chatter, not chat content — swallow at the adapter boundary.
_RUNTIME_NOTICE_PATTERNS = (
    re.compile(r"^⚡\s+Interrupting current task\b"),
    re.compile(r"^⏩\s+Steered into current run\b"),
    re.compile(r"^↪\s+Redirected current run\b"),
    re.compile(r"^⏳\s+Queued for the next turn\b"),
    re.compile(r"^⏳\s+Subagent working\b"),
    re.compile(r"^⏳\s+Compressing context\b"),
    re.compile(r"^⏳\s+Gateway\b"),
)


def _first_line(content: str) -> str:
    stripped = (content or "").strip()
    if not stripped:
        return ""
    return stripped.split("\n", 1)[0].strip()


def detect_gateway_runtime_notice(content: str) -> Optional[str]:
    """Return stripped text when *content* is a busy-ack / queue / steer / drain notice.

    Returns ``None`` for normal messages (including "Still working" progress
    lines, which remain thinking-card candidates via :func:`detect_agent_status`).
    """
    first_line = _first_line(content)
    if not first_line:
        return None
    for pattern in _RUNTIME_NOTICE_PATTERNS:
        if pattern.match(first_line):
            return (content or "").strip()
    return None


def detect_agent_status(content: str) -> Optional[str]:
    """Return the cleaned status text if *content* is a gateway status message.

    Returns ``None`` when the content is a normal message.  The returned string
    is the original content (stripped) — it becomes the thinking card body.
    """
    if not content:
        return None

    stripped = content.strip()
    if not stripped:
        return None

    first_line = stripped.split("\n", 1)[0].strip()
    for pattern in _STATUS_PATTERNS:
        if pattern.match(first_line):
            return stripped

    return None


def build_agent_status_channel_data(status_text: str) -> Dict[str, Any]:
    """Build ``channel_data.grix.thinking`` for the backend.

    Reuses the existing ``thinking`` card type so the status renders as a
    lightweight card on clients that already support it, with zero backend or
    client changes.
    """
    return {
        "grix": {
            "thinking": {
                "content": status_text,
            },
        },
    }
