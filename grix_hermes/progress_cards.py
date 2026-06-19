"""Render Hermes gateway status lines as progress-bar cards.

The gateway emits periodic status notifications in two formats::

    ⏳ Queued for the next turn (2 min elapsed, iteration 3/10, running: bash).
    ⏳ Working — 3 min — iteration 9/90, process

Both carry an ``iteration N/M`` progress and elapsed time. This module
detects either format and surfaces them as a ``grix://card/progress``
card with a percent bar.
"""

from __future__ import annotations

import re
from typing import Optional

from .card_links import build_progress_card

_PROGRESS_RE = re.compile(
    r"^⏳\s+(?:Queued for the next turn|Working)\b", re.IGNORECASE,
)
_ITERATION_RE = re.compile(r"iteration\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
# "2 min elapsed" 或 "— 3 min —"
_ELAPSED_RE = re.compile(r"(\d+)\s*min(?:ute)?s?(?:\s+elapsed)?", re.IGNORECASE)


def build_queue_progress_card(status_text: str) -> Optional[str]:
    """Return a ``grix://card/progress`` link for a queue/working status line.

    Matches both "Queued for the next turn" and "Working" gateway formats.
    Returns ``None`` for other status lines so the caller falls back to the
    agent-status thinking card path.
    """
    if not status_text:
        return None
    stripped = status_text.strip()
    if not _PROGRESS_RE.search(stripped):
        return None

    percent: Optional[int] = None
    parts = []

    it = _ITERATION_RE.search(stripped)
    if it:
        current = int(it.group(1))
        total = int(it.group(2))
        parts.append(f"第{current}/{total}轮")
        if total > 0:
            percent = max(0, min(100, round(current / total * 100)))

    el = _ELAPSED_RE.search(stripped)
    if el:
        parts.append(f"已跑{int(el.group(1))}分钟")

    if parts:
        label = "处理中 · " + " · ".join(parts)
    else:
        label = "排队等待处理"

    return build_progress_card(label, percent)
