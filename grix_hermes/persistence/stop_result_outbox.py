"""Crash-safe outbox for tokenized event_stop_result packets."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .atomic_json import atomic_unlink, atomic_write_json, read_json_object

FILE_VERSION = 1
_DURABLE_STATUSES = frozenset({"stopped", "already_finished"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def entry_key(payload: Dict[str, Any]) -> str:
    event_id = str(payload.get("event_id") or "").strip()
    stop_id = str(payload.get("stop_id") or "").strip()
    return f"{event_id}\0{stop_id}"


def _is_durable_stop_result(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    return bool(
        str(payload.get("event_id") or "").strip()
        and str(payload.get("stop_id") or "").strip()
        and str(payload.get("terminal_commit_token") or "").strip()
        and str(payload.get("status") or "").strip() in _DURABLE_STATUSES
    )


@dataclass
class StopResultOutboxEntry:
    key: str
    payload: Dict[str, Any]
    generation: int
    created_at: int
    updated_at: int
    attempts: int = 0
    next_attempt_at: int = 0
    last_error: Optional[str] = None

    def clone(self) -> "StopResultOutboxEntry":
        return StopResultOutboxEntry(
            key=self.key,
            payload=deepcopy(self.payload),
            generation=self.generation,
            created_at=self.created_at,
            updated_at=self.updated_at,
            attempts=self.attempts,
            next_attempt_at=self.next_attempt_at,
            last_error=self.last_error,
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "key": self.key,
            "payload": deepcopy(self.payload),
            "generation": self.generation,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "attempts": self.attempts,
            "nextAttemptAt": self.next_attempt_at,
        }
        if self.last_error is not None:
            data["lastError"] = self.last_error
        return data


class StopResultOutbox:
    def __init__(self, file_path: Optional[str] = None):
        self._file_path = file_path
        self._pending: Dict[str, StopResultOutboxEntry] = {}
        self._load()

    def enqueue(self, payload: Dict[str, Any]) -> StopResultOutboxEntry:
        if not _is_durable_stop_result(payload):
            raise ValueError(
                "stop result outbox requires event_id, stop_id, terminal token, and valid status"
            )
        normalized = {
            **payload,
            "event_id": str(payload["event_id"]).strip(),
            "stop_id": str(payload["stop_id"]).strip(),
            "terminal_commit_token": str(payload["terminal_commit_token"]).strip(),
        }
        key = entry_key(normalized)
        previous = self._pending.get(key)
        if previous:
            if (
                previous.payload.get("status") != normalized.get("status")
                or previous.payload.get("terminal_commit_token")
                != normalized.get("terminal_commit_token")
            ):
                raise ValueError(
                    f"stop result changed for event={normalized['event_id']} "
                    f"stop={normalized['stop_id']}"
                )
            return previous.clone()

        now = _now_ms()
        entry = StopResultOutboxEntry(
            key=key,
            payload=normalized,
            generation=1,
            created_at=now,
            updated_at=now,
            attempts=0,
            next_attempt_at=0,
        )
        self._pending[key] = entry
        try:
            self._persist()
        except Exception:
            self._pending.pop(key, None)
            raise
        return entry.clone()

    def list_pending(self) -> List[StopResultOutboxEntry]:
        return [entry.clone() for entry in self._pending.values()]

    def is_current(self, entry: StopResultOutboxEntry) -> bool:
        current = self._pending.get(entry.key)
        return (
            current is not None
            and current.generation == entry.generation
            and current.payload.get("status") == entry.payload.get("status")
            and current.payload.get("terminal_commit_token")
            == entry.payload.get("terminal_commit_token")
        )

    def acknowledge(
        self,
        entry: StopResultOutboxEntry,
        event_id: Optional[str],
        terminal_commit_token: Optional[str],
        terminal_committed: Optional[bool],
    ) -> bool:
        if (
            str(event_id or "").strip() != entry.payload.get("event_id")
            or str(terminal_commit_token or "").strip()
            != entry.payload.get("terminal_commit_token")
            or terminal_committed is not True
            or not self.is_current(entry)
        ):
            return False
        current = self._pending[entry.key]
        self._pending.pop(entry.key, None)
        try:
            self._persist()
        except Exception:
            self._pending[entry.key] = current
            raise
        return True

    def record_retry(
        self, entry: StopResultOutboxEntry, next_attempt_at: int, error: str
    ) -> bool:
        current = self._pending.get(entry.key)
        if not current or current.generation != entry.generation:
            return False
        previous = current.clone()
        current.attempts += 1
        current.updated_at = _now_ms()
        current.next_attempt_at = next_attempt_at
        current.last_error = error
        try:
            self._persist()
        except Exception:
            self._pending[entry.key] = previous
            raise
        return True

    def discard(self, entry: StopResultOutboxEntry) -> bool:
        current = self._pending.get(entry.key)
        if not current or current.generation != entry.generation:
            return False
        self._pending.pop(entry.key, None)
        try:
            self._persist()
        except Exception:
            self._pending[entry.key] = current
            raise
        return True

    def _load(self) -> None:
        try:
            file = read_json_object(self._file_path)
        except RuntimeError as exc:
            raise RuntimeError(f"stop result outbox load failed: {exc}") from exc
        if file is None:
            return
        if file.get("version") != FILE_VERSION or not isinstance(file.get("pending"), dict):
            raise RuntimeError("stop result outbox load failed: unsupported or invalid file")
        for key, raw in file["pending"].items():
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload")
            if (
                not _is_durable_stop_result(payload)
                or key != raw.get("key")
                or key != entry_key(payload)
            ):
                continue
            generation = raw.get("generation")
            created_at = raw.get("createdAt")
            updated_at = raw.get("updatedAt")
            attempts = raw.get("attempts")
            next_attempt = raw.get("nextAttemptAt")
            self._pending[key] = StopResultOutboxEntry(
                key=key,
                payload=deepcopy(payload),
                generation=(
                    int(generation)
                    if isinstance(generation, int) and generation > 0
                    else 1
                ),
                created_at=(
                    int(created_at) if isinstance(created_at, (int, float)) else _now_ms()
                ),
                updated_at=(
                    int(updated_at) if isinstance(updated_at, (int, float)) else _now_ms()
                ),
                attempts=(
                    int(attempts) if isinstance(attempts, int) and attempts >= 0 else 0
                ),
                next_attempt_at=(
                    int(next_attempt) if isinstance(next_attempt, (int, float)) else 0
                ),
                last_error=(
                    str(raw["lastError"])
                    if isinstance(raw.get("lastError"), str)
                    else None
                ),
            )

    def _persist(self) -> None:
        if not self._file_path:
            return
        if not self._pending:
            atomic_unlink(self._file_path)
            return
        atomic_write_json(
            self._file_path,
            {
                "version": FILE_VERSION,
                "pending": {
                    key: entry.to_dict() for key, entry in self._pending.items()
                },
            },
        )
