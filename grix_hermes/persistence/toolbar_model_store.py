"""Persisted toolbar model / provider selection (session + agent global).

Mirrors the connector's DSH split: ``SessionBindingStore`` keeps the per
session choice and ``AgentGlobalConfigStore`` keeps the last toolbar choice
so brand-new sessions inherit it.  Both live in one JSON file per agent.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, Optional

from .atomic_json import atomic_write_json, read_json_object

logger = logging.getLogger(__name__)

FILE_VERSION = 1
# 会话条目上限（LRU 淘汰），避免长期运行下文件无界增长。
MAX_SESSIONS = 500


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
    """Session-keyed model choices plus a per-owner ``global`` last choice.

    ``global`` is keyed by owner_key (empty string for the agent owner) so a
    sharee's toolbar choice does not leak into the owner's new sessions,
    mirroring the connector's per-agent AgentGlobalConfigStore.
    """

    def __init__(self, file_path: Optional[str] = None, *, max_sessions: int = MAX_SESSIONS):
        self._file_path = file_path
        self._max_sessions = max(1, max_sessions)
        self._sessions: "OrderedDict[str, Dict[str, str]]" = OrderedDict()
        self._global: Dict[str, Dict[str, str]] = {}
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
        raw_global = data.get("global")
        if isinstance(raw_global, dict):
            legacy = _normalize_entry(raw_global)
            if legacy is not None:
                self._global[""] = legacy
            else:
                for owner_key, value in raw_global.items():
                    entry = _normalize_entry(value)
                    if entry is not None:
                        self._global[str(owner_key)] = entry

    def _save(self) -> None:
        if not self._file_path:
            return
        try:
            atomic_write_json(
                self._file_path,
                {
                    "version": FILE_VERSION,
                    "sessions": dict(self._sessions),
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

    def get_global(self, owner_key: str = "") -> Optional[Dict[str, str]]:
        entry = self._global.get(str(owner_key or ""))
        return dict(entry) if entry else None

    def set_session(
        self,
        key: str,
        entry: Dict[str, Any],
        *,
        owner_key: str = "",
        update_global: bool = True,
    ) -> None:
        normalized = _normalize_entry(entry)
        if not key or normalized is None:
            return
        self._sessions[key] = normalized
        self._sessions.move_to_end(key)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        if update_global:
            self._global[str(owner_key or "")] = dict(normalized)
        self._save()
