"""Terminal outbox path helpers (parity with grix-connector manager / share-config)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .skill_paths import resolve_grix_home


def normalize_segment(value: str, fallback: str = "default") -> str:
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower())
    cleaned = re.sub(r"-+", "-", cleaned).strip("-")
    return cleaned or fallback


def suffix_shared_path(path: Optional[str], shared_owner_id: str) -> Optional[str]:
    """Insert `.shared.{uid}` before the file extension (path isolation per sharee)."""
    if not path:
        return path
    owner = str(shared_owner_id or "").strip()
    if not owner:
        return path
    p = Path(path)
    if p.suffix:
        return str(p.with_name(f"{p.stem}.shared.{owner}{p.suffix}"))
    return f"{path}.shared.{owner}"


def build_terminal_outbox_path(agent_name: str, agent_id: str) -> str:
    name_segment = normalize_segment(agent_name, "agent")
    id_segment = normalize_segment(agent_id, "unknown")
    data_dir = resolve_grix_home() / "data"
    return str(data_dir / f"terminal-outbox-{name_segment}-{id_segment}.json")


def resolve_terminal_sidecar_paths(
    terminal_outbox_path: Optional[str],
    *,
    token_path: Optional[str] = None,
    stop_path: Optional[str] = None,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not terminal_outbox_path:
        return None, token_path, stop_path
    return (
        terminal_outbox_path,
        token_path or f"{terminal_outbox_path}.tokens",
        stop_path or f"{terminal_outbox_path}.stops",
    )
