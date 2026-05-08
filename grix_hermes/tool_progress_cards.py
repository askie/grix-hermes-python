"""Tool execution card support for the Grix adapter.

Detects tool progress messages from the Hermes framework and builds
structured channel_data so the backend can render tool_execution cards.

Hermes progress format (from gateway/run.py progress_callback):
  - "all"/"new" mode:  emoji tool_name: "preview"  or  emoji tool_name...
  - "verbose" mode:    emoji tool_name(keys)\\nargs_json
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

_TOOL_PROGRESS_RE = re.compile(
    r"^[^\w\s]+"           # leading emoji(s) (non-word, non-space)
    r"\s+"
    r"([\w][\w\-]*)"        # tool name  (group 1)
    r"\s*"
    r"(?:"
    r':\s*"([^"]*)"'       # : "preview"  (group 2)
    r"|(\.\.\.+)"           # ...          (group 3)
    r"|\(([^)]*)\)"         # (keys)       (group 4)
    r")"
    r"\s*(?:\(×\d+\))?$",  # optional dedup counter (×N)
)


def detect_tool_progress(content: str) -> Optional[Tuple[str, str]]:
    """Return (tool_name, preview) if *content* is a tool-progress message.

    Handles both single-line (``all``/``new`` mode) and multi-line
    (``verbose`` mode or accumulated edits) tool progress content.
    The first line must match the tool-progress pattern; additional
    lines (verbose args or more tool lines) are allowed.
    """
    if not content:
        return None

    lines = content.strip().split("\n")
    # First line must be a tool-progress line.
    if not _TOOL_PROGRESS_RE.match(lines[0].strip()):
        return None

    # Extract from the last tool-progress line (most recent tool call).
    for line in reversed(lines):
        m = _TOOL_PROGRESS_RE.match(line.strip())
        if m:
            tool_name = m.group(1)
            preview = m.group(2) or m.group(4) or ""
            return tool_name, preview

    return None


def build_tool_execution_channel_data(tool_name: str, preview: str) -> Dict[str, Any]:
    """Build ``channel_data.grix.toolExecution`` for the backend."""
    summary = f"{tool_name}: {preview}" if preview else tool_name
    return {
        "grix": {
            "toolExecution": {
                "summary_text": summary,
            },
        },
    }
