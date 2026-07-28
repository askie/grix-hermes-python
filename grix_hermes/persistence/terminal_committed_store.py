"""Crash-safe tombstones for settled terminal event_ids (ACK or dead-letter).

Hermes replaces GrixTransportClient on reconnect, so an in-memory committed
set alone cannot prevent memory replay from re-enqueuing an already-settled
terminal. Persist a bounded FIFO of settled ids next to the outbox.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from .atomic_json import atomic_unlink, atomic_write_json, read_json_object

FILE_VERSION = 1
MAX_COMMITTED = 4096


class TerminalCommittedStore:
    def __init__(self, file_path: Optional[str] = None, *, max_size: int = MAX_COMMITTED):
        self._file_path = file_path
        self._max_size = max(1, max_size)
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._load()

    def has(self, event_id: str) -> bool:
        return str(event_id or "").strip() in self._ids

    def add(self, event_id: str) -> None:
        eid = str(event_id or "").strip()
        if not eid:
            return
        if eid in self._ids:
            self._ids.move_to_end(eid)
            return
        self._ids[eid] = None
        while len(self._ids) > self._max_size:
            self._ids.popitem(last=False)
        self._persist()

    def _load(self) -> None:
        try:
            file = read_json_object(self._file_path)
        except RuntimeError as exc:
            raise RuntimeError(f"terminal committed store load failed: {exc}") from exc
        if file is None:
            return
        if file.get("version") != FILE_VERSION or not isinstance(file.get("ids"), list):
            raise RuntimeError(
                "terminal committed store load failed: unsupported or invalid file"
            )
        for raw in file["ids"]:
            eid = str(raw or "").strip()
            if eid:
                self._ids[eid] = None
        while len(self._ids) > self._max_size:
            self._ids.popitem(last=False)

    def _persist(self) -> None:
        if not self._file_path:
            return
        if not self._ids:
            atomic_unlink(self._file_path)
            return
        atomic_write_json(
            self._file_path,
            {"version": FILE_VERSION, "ids": list(self._ids.keys())},
        )
