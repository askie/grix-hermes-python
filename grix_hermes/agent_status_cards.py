"""Agent status card support for the Grix adapter.

The Hermes gateway (core) emits periodic *system status* notifications while a
long-running turn is in flight — "still working", inactivity warnings, queued /
gateway-busy notices.  These are not agent replies; surfacing them as normal
message bubbles is noisy.  This module detects those status lines so the adapter
can tag them with ``channel_data.grix.thinking`` and let the backend/client
render them as a lightweight thinking card instead of a formal bubble.

Source of the status strings (gateway/run.py):
  - "⏳ Still working... (N min elapsed — iteration X/Y, running: tool)"
  - "⚠️ No activity for N min. ..."
  - "⏳ Queued for the next turn ..."
  - "⏳ Gateway is running ..." / "⏳ Gateway running — queued ..."
  - "⏳ Agent is running — `/cmd` can't run ..."
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
    re.compile(r"^⚠️\s+No activity for\b"),
    re.compile(r"^⏳\s+Queued for the next turn\b"),
    re.compile(r"^⏳\s+Gateway\b"),
    re.compile(r"^⏳\s+Agent is running\b"),
)


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
