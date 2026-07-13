"""Live check against the installed hermes — run with the hermes venv.

Two things unit tests cannot cover, because they need hermes importable:

1. The fallback verb table still mirrors ``agent.display._TOOL_VERBS``.
   A drift here means friendly labels for the new/renamed tools would leak
   into chat as plain text on any host where hermes is not importable.
2. The official seam works end to end: a ``ToolCallChunk`` routed through
   ``GatewayEventDispatcher`` → ``GrixAdapter.format_tool_event`` produces a
   line that ``detect_tool_progress`` resolves back to the same tool.
   Upstream has not wired the dispatcher into gateway/run.py yet; this is
   what proves the adapter is ready for when it does.

Usage:  ~/.hermes/hermes-agent/.venv/bin/python3 tests/run_tool_progress_dispatch_check.py
"""

from __future__ import annotations

import sys

from grix_hermes.tool_progress_cards import (
    _FALLBACK_TOOL_VERBS,
    _FALLBACK_VERBS_FOR_CONNECTOR,
    _FALLBACK_VERBS_NO_PREVIEW,
    detect_tool_progress,
)

failures: list[str] = []


def check_verb_table_in_sync() -> None:
    from agent.display import (
        _TOOL_VERBS,
        _TOOL_VERBS_FOR_CONNECTOR,
        _TOOL_VERBS_NO_PREVIEW,
    )

    if dict(_TOOL_VERBS) != _FALLBACK_TOOL_VERBS:
        missing = set(_TOOL_VERBS) - set(_FALLBACK_TOOL_VERBS)
        extra = set(_FALLBACK_TOOL_VERBS) - set(_TOOL_VERBS)
        changed = {
            t for t in set(_TOOL_VERBS) & set(_FALLBACK_TOOL_VERBS)
            if _TOOL_VERBS[t] != _FALLBACK_TOOL_VERBS[t]
        }
        failures.append(
            f"verb table drift — missing={sorted(missing)} extra={sorted(extra)} changed={sorted(changed)}"
        )
    if set(_TOOL_VERBS_NO_PREVIEW) != set(_FALLBACK_VERBS_NO_PREVIEW):
        failures.append("no-preview set drift")
    if set(_TOOL_VERBS_FOR_CONNECTOR) != set(_FALLBACK_VERBS_FOR_CONNECTOR):
        failures.append("connector set drift")
    print(f"[1] verb table: {len(_TOOL_VERBS)} tools, in sync with fallback copy")


def check_live_gateway_labels() -> None:
    """Every built-in tool's real gateway line must resolve back to a tool."""
    from agent.display import get_tool_emoji, tool_verb_connector, verb_drops_preview
    from agent.display import _TOOL_VERBS

    for tool, verb in _TOOL_VERBS.items():
        preview = "sample-arg"
        emoji = get_tool_emoji(tool, default="⚙️")
        if verb_drops_preview(tool):
            line = f"{emoji} {verb}"
            expected_preview = ""
        else:
            line = f"{emoji} {verb}{tool_verb_connector(tool)}{preview}"
            expected_preview = preview
        hit = detect_tool_progress(line)
        if hit is None:
            failures.append(f"{tool}: gateway line not detected: {line!r}")
            continue
        got_tool, got_preview = hit
        if got_preview != expected_preview:
            failures.append(f"{tool}: preview mismatch {got_preview!r} != {expected_preview!r}")
        # Shared verbs (Reading = read_file / web_extract) legitimately resolve
        # to the tie-break winner; only assert the label round-trips to *a* tool.
    print(f"[2] all {len(_TOOL_VERBS)} live gateway labels detected as tool progress")


def check_dispatcher_seam() -> None:
    from gateway.stream_dispatch import GatewayEventDispatcher
    from gateway.stream_events import ToolCallChunk

    from grix_hermes.adapter import GrixAdapter

    lines: list[str] = []
    dispatcher = GatewayEventDispatcher(
        GrixAdapter.__new__(GrixAdapter),  # format_tool_event needs no instance state
        sink=None,
        enqueue_tool_line=lines.append,
        tool_mode="all",
        preview_max_len=40,
    )
    dispatcher.dispatch(ToolCallChunk(tool_name="read_file", preview="docs/api.md", args={"path": "docs/api.md"}))
    dispatcher.dispatch(ToolCallChunk(tool_name="web_search", preview="grix hermes", args={"query": "grix hermes"}))

    expected = [('read_file', 'docs/api.md'), ('web_search', 'grix hermes')]
    got = [detect_tool_progress(line) for line in lines]
    if got != expected:
        failures.append(f"dispatcher seam: {got} != {expected} (lines={lines})")
    print(f"[3] dispatcher seam: {lines} -> {got}")


check_verb_table_in_sync()
check_live_gateway_labels()
check_dispatcher_seam()

if failures:
    print("\nFAILED:")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("\nOK — all checks passed")
