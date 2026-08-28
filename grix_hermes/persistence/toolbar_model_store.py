"""Persisted toolbar model / provider selection (session + agent global).

Mirrors the connector's DSH split: ``SessionBindingStore`` keeps the per
session choice and ``AgentGlobalConfigStore`` keeps the last toolbar choice
so brand-new sessions inherit it.  Both live in one JSON file per agent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from .atomic_json import atomic_write_json, read_json_object

logger = logging.getLogger(__name__)

FILE_VERSION = 1
_FIELDS = ("model_id", "provider", "display_label")


def _normalize_entry(value: Any) -> Optional[Dict[str, str]]:
    if not isinstance(value, dict):
        return None
    model_id = str(value.get("model_id") or "").strip()
    if not model_id:
        return None
    return {
        "model_id": model_id,
        "provider": str(value.get("provider") or "").strip(),
        "display_label": str(value.get("display_label") or "").strip(),
    }


class ToolbarModelStore:
    """Session-keyed model choices plus one agent-wide ``global`` default."""

    def __init__(self, file_path: Optional[str] = None):
        self._file_path = file_path
        self._sessions: Dict[str, Dict[str, str]] = {}
        self._global: Optional[Dict[str, str]] = None
        self._load()

    # -- load / save -------------------------------------------------------
    def _load(self) -> None:
        if not self._file_path:
            return
        try:
            data = read_json_object(self._file_path)
        except Exception:
            logger.debug("toolbar model store load failed: %s", self._file_path, exc_info=True)
            return
        if not isinstance(data, dict):
            return
        sessions = data.get("sessions")
        if isinstance(sessions, dict):
            for key, value in sessions.items():
                entry = _normalize_entry(value)
                if key and entry is not None:
                    self._sessions[str(key)] = entry
        self._global = _normalize_entry(data.get("global"))

    def _save(self) -> None:
        if not self._file_path:
            return
        try:
            atomic_write_json(
                self._file_path,
                {
                    "version": FILE_VERSION,
                    "sessions": self._sessions,
                    "global": self._global,
                },
            )
        except Exception:
            logger.warning("toolbar model store save failed: %s", self._file_path, exc_info=True)

    # -- accessors ---------------------------------------------------------
    @property
    def sessions(self) -> Dict[str, Dict[str, str]]:
        """Live session map (the adapter keeps using this dict in memory)."""
        return self._sessions

    def get_session(self, key: str) -> Optional[Dict[str, str]]:
        return self._sessions.get(key)

    def get_global(self) -> Optional[Dict[str, str]]:
        return dict(self._global) if self._global else None

    def set_session(self, key: str, entry: Dict[str, Any], *, update_global: bool = True) -> None:
        normalized = _normalize_entry(entry)
        if not key or normalized is None:
            return
        self._sessions[key] = normalized
        if update_global:
            self._global = dict(normalized)
        self._save()

    def clear_session(self, key: str) -> None:
        if self._sessions.pop(key, None) is not None:
            self._save()
