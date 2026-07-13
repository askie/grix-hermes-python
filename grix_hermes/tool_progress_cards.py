"""Tool execution card support for the Grix adapter.

Detects tool progress messages from the Hermes framework and builds
structured channel_data so the backend can render tool_execution cards.

Hermes emits tool progress in two shapes (gateway/run.py progress_callback):

  1. Friendly labels (default since hermes #55166) — built-in tools only:
       emoji verb[ for ]preview     e.g.  📖 Reading docs/api.md
                                          🔍 Searching the web for grix
       emoji verb                   e.g.  📋 Listing skills
  2. Raw form — custom / plugin / MCP tools, and any tool when
     ``display.friendly_tool_labels`` is off:
       emoji tool_name: "preview"   /   emoji tool_name...
       emoji tool_name(keys)\\nargs_json      ("verbose" mode)

The verb table is read from hermes itself (``agent.display``) so a wording
change upstream is picked up without touching this file; the copy below is
the fallback for environments where that module is not importable (unit
tests, or an upstream refactor of the private table).

Hermes also defines a structured seam for this — ``ToolCallChunk`` events
routed through ``BasePlatformAdapter.format_tool_event`` — which the adapter
implements.  Until upstream wires ``GatewayEventDispatcher`` into
``gateway/run.py``, tool progress still reaches the adapter as plain text,
which is what the detectors here are for.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_TOOL_PROGRESS_RE = re.compile(
    r"^[^\w\s]+"           # leading emoji(s) (non-word, non-space)
    r"\s+"
    r"([\w][\w\-]*)"        # tool name  (group 1)
    r"\s*"
    r"(?:"
    r':\s*"(.*)"'          # : "preview"  (group 2, greedy — tolerates inner quotes)
    r"|(\.\.\.+)"           # ...          (group 3)
    r"|\(([^)]*)\)"         # (keys)       (group 4)
    r")"
    r"\s*(?:\(×\d+\))?$",  # optional dedup counter (×N)
)

# Matches Claude Code hook notification lines whose action name may contain
# spaces, e.g.:  💾 Self-improvement review: Memory updated
_HOOK_STATUS_RE = re.compile(
    r"^[^\w\s]+"               # leading emoji(s)
    r"\s+"
    r"([A-Za-z][\w\- ]*\w)"   # action name — starts/ends with word char, spaces allowed
    r"\s*:\s*"
    r"(.+)$",
)

# Splits "emoji rest-of-line" for the friendly-label form.
_EMOJI_PREFIX_RE = re.compile(r"^[^\w\s]+\s+(.+)$")

# Trailing dedup counter appended by the gateway on repeated identical lines.
_DEDUP_SUFFIX_RE = re.compile(r"\s*\(×\d+\)$")

# Mirror of hermes ``agent.display._TOOL_VERBS`` — used only when that module
# cannot be imported.  Keep in sync when adding tools upstream.
_FALLBACK_TOOL_VERBS: Dict[str, str] = {
    "web_search": "Searching the web",
    "web_extract": "Reading",
    "browser_navigate": "Browsing",
    "browser_click": "Clicking",
    "browser_type": "Typing",
    "read_file": "Reading",
    "write_file": "Writing",
    "patch": "Editing",
    "search_files": "Searching files",
    "terminal": "Running",
    "execute_code": "Running code",
    "image_generate": "Generating image",
    "video_generate": "Generating video",
    "text_to_speech": "Generating speech",
    "vision_analyze": "Looking at the image",
    "session_search": "Searching past sessions",
    "skill_view": "Reading skill",
    "skills_list": "Listing skills",
    "skill_manage": "Updating skill",
    "delegate_task": "Delegating",
    "cronjob": "Scheduling",
    "clarify": "Asking",
    "memory": "Updating memory",
    "todo": "Updating tasks",
}
_FALLBACK_VERBS_NO_PREVIEW = frozenset({"skills_list", "session_search"})
_FALLBACK_VERBS_FOR_CONNECTOR = frozenset({"web_search", "search_files"})

# Several tools share a verb ("Reading" is both read_file and web_extract), so
# the label alone cannot name the tool.  Resolve such collisions toward the
# tool listed first here, then refine with the preview (a URL means the web
# tool, not the file one).
_VERB_TIE_BREAK: Tuple[str, ...] = ("read_file", "terminal", "web_search")
_URL_TOOL_BY_FILE_TOOL: Dict[str, str] = {"read_file": "web_extract"}

# (label_prefix, tool_name, drops_preview), longest prefix first so
# "Searching the web for " wins over "Searching files for ".
_VerbIndex = List[Tuple[str, str, bool]]
_verb_index_cache: Optional[_VerbIndex] = None


def _verb_index() -> _VerbIndex:
    """Build the label→tool lookup, preferring hermes' own verb table."""
    global _verb_index_cache
    if _verb_index_cache is not None:
        return _verb_index_cache

    try:
        from agent.display import (  # type: ignore[import-not-found]
            _TOOL_VERBS,
            tool_verb_connector,
            verb_drops_preview,
        )

        verbs = dict(_TOOL_VERBS)

        def _connector(tool: str) -> str:
            return tool_verb_connector(tool)

        def _drops(tool: str) -> bool:
            return verb_drops_preview(tool)
    except Exception:
        verbs = dict(_FALLBACK_TOOL_VERBS)

        def _connector(tool: str) -> str:
            return " for " if tool in _FALLBACK_VERBS_FOR_CONNECTOR else " "

        def _drops(tool: str) -> bool:
            return tool in _FALLBACK_VERBS_NO_PREVIEW

    def _rank(tool: str) -> int:
        return _VERB_TIE_BREAK.index(tool) if tool in _VERB_TIE_BREAK else len(_VERB_TIE_BREAK)

    index: _VerbIndex = []
    for tool, verb in verbs.items():
        if _drops(tool):
            index.append((verb, tool, True))
        else:
            index.append((verb + _connector(tool), tool, False))
    index.sort(key=lambda entry: (-len(entry[0]), _rank(entry[1])))

    _verb_index_cache = index
    return index


def _detect_verb_line(line: str) -> Optional[Tuple[str, str]]:
    """Return (tool_name, preview) for a friendly-label progress line."""
    m = _EMOJI_PREFIX_RE.match(line)
    if not m:
        return None
    rest = _DEDUP_SUFFIX_RE.sub("", m.group(1)).strip()
    if not rest:
        return None

    for prefix, tool_name, drops_preview in _verb_index():
        if drops_preview:
            if rest == prefix:
                return tool_name, ""
        elif rest.startswith(prefix):
            preview = rest[len(prefix):].strip()
            if not preview:
                continue
            if preview.startswith(("http://", "https://")):
                tool_name = _URL_TOOL_BY_FILE_TOOL.get(tool_name, tool_name)
            return tool_name, preview
    return None


def _detect_progress_line(line: str) -> Optional[Tuple[str, str]]:
    """Return (tool_name, preview) for a single progress line, either shape."""
    stripped = line.strip()
    if not stripped:
        return None

    m = _TOOL_PROGRESS_RE.match(stripped)
    if m:
        return m.group(1), m.group(2) or m.group(4) or ""

    return _detect_verb_line(stripped)


def detect_tool_progress(content: str) -> Optional[Tuple[str, str]]:
    """Return (tool_name, preview) if *content* is a tool-progress message.

    Handles single-line (``all``/``new`` mode) and multi-line (``verbose``
    mode or accumulated edits) content, in both the friendly-label and raw
    shapes.  The first line must be a tool-progress line; additional lines
    (verbose args, or further tool lines) are allowed, and the tool reported
    is the one on the last progress line — the most recent call.
    """
    if not content:
        return None

    lines = content.strip().split("\n")
    if _detect_progress_line(lines[0]) is None:
        return None

    for line in reversed(lines):
        hit = _detect_progress_line(line)
        if hit:
            return hit

    return None


def detect_hook_status(content: str) -> Optional[Tuple[str, str]]:
    """Return (action_name, description) if *content* is a single-line hook notification.

    Handles Claude Code hook messages like:
        💾 Self-improvement review: Memory updated
    where the action name may contain spaces (unlike ``detect_tool_progress``).
    Only matches single-line content.
    """
    if not content:
        return None
    stripped = content.strip()
    if "\n" in stripped:
        return None
    m = _HOOK_STATUS_RE.match(stripped)
    if not m:
        return None
    return m.group(1), m.group(2)


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
