"""Render Hermes gateway *queue* status lines as progress-bar cards.

The Hermes gateway emits a queue notification while the agent waits for its
next turn, e.g. ``⏳ Queued for the next turn (iteration 4/8).``  Instead of
a plain status card, surface it as a progress card (one-line label + percent),
reusing the same backend pass-through that other grix:// card links rely on.
"""

from __future__ import annotations

import re
from typing import Optional

from .card_links import build_progress_card

# 仅「排队等待下一轮」这类消息渲染为进度卡。
_QUEUE_RE = re.compile(r"^⏳\s+Queued for the next turn\b")
# 形如 "(iteration 4/8)" 的轮次进度。
_ITERATION_RE = re.compile(r"iteration\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def build_queue_progress_card(status_text: str) -> Optional[str]:
    """Return a ``grix://card/progress`` link if [status_text] is a queue notice.

    Returns ``None`` for any non-queue status line so the caller can fall back
    to the existing agent-status card path.
    """
    if not status_text:
        return None
    stripped = status_text.strip()
    if not _QUEUE_RE.search(stripped):
        return None

    label = "排队等待下一轮"
    percent: Optional[int] = None
    match = _ITERATION_RE.search(stripped)
    if match:
        current = int(match.group(1))
        total = int(match.group(2))
        label = f"排队等待下一轮 ({current}/{total})"
        if total > 0:
            percent = max(0, min(100, round(current / total * 100)))

    return build_progress_card(label, percent)
