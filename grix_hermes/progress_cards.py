"""Render Hermes gateway *queue* status lines as progress-bar cards.

When a user message arrives while the agent is still busy with the previous
turn, the gateway replies with a queue notice, e.g.::

    ⏳ Queued for the next turn (2 min elapsed, iteration 3/10, running: bash). I'll respond once the current task finishes.

The parenthesised detail is built by the gateway from the running agent's
structured activity summary (``api_call_count`` / ``max_iterations`` /
elapsed minutes). Instead of showing the raw line, surface it as a progress
card: a one-line label plus a percent derived from ``iteration N/M``, reusing
the same backend pass-through that other ``grix://`` card links rely on.
"""

from __future__ import annotations

import re
from typing import Optional

from .card_links import build_progress_card

# 仅「排队等待下一轮」这类消息渲染为进度卡。
_QUEUE_RE = re.compile(r"^⏳\s+Queued for the next turn\b", re.IGNORECASE)
# 形如 "iteration 3/10" 的轮次进度（来自网关 activity_summary）。
_ITERATION_RE = re.compile(r"iteration\s+(\d+)\s*/\s*(\d+)", re.IGNORECASE)
# 形如 "2 min elapsed" 的已跑时间。
_ELAPSED_RE = re.compile(r"(\d+)\s*min(?:ute)?s?\s+elapsed", re.IGNORECASE)


def build_queue_progress_card(status_text: str) -> Optional[str]:
    """Return a ``grix://card/progress`` link if *status_text* is a queue notice.

    The percent comes from the gateway's ``iteration N/M`` activity参数; the
    label also surfaces the elapsed minutes when present. Returns ``None`` for
    any non-queue status line so the caller falls back to the agent-status path.
    """
    if not status_text:
        return None
    stripped = status_text.strip()
    if not _QUEUE_RE.search(stripped):
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
