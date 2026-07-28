"""Synchronous, atomically persisted outbox for terminal event_result packets."""

from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .atomic_json import atomic_write_json, read_json_object

FILE_VERSION = 1
MAX_DEAD_LETTERS = 1000
_TERMINAL_STATUSES = frozenset({"responded", "failed", "canceled"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clone_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return deepcopy(payload)


def _is_event_result_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    event_id = str(value.get("event_id") or "").strip()
    status = str(value.get("status") or "").strip()
    return bool(event_id) and status in _TERMINAL_STATUSES


@dataclass
class TerminalOutboxEntry:
    payload: Dict[str, Any]
    generation: int
    created_at: int
    updated_at: int
    attempts: int = 0
    next_attempt_at: int = 0
    delivery_started_at: Optional[int] = None
    last_error: Optional[str] = None

    def clone(self) -> "TerminalOutboxEntry":
        return TerminalOutboxEntry(
            payload=_clone_payload(self.payload),
            generation=self.generation,
            created_at=self.created_at,
            updated_at=self.updated_at,
            attempts=self.attempts,
            next_attempt_at=self.next_attempt_at,
            delivery_started_at=self.delivery_started_at,
            last_error=self.last_error,
        )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "payload": _clone_payload(self.payload),
            "generation": self.generation,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "attempts": self.attempts,
            "nextAttemptAt": self.next_attempt_at,
        }
        if self.delivery_started_at is not None:
            data["deliveryStartedAt"] = self.delivery_started_at
        if self.last_error is not None:
            data["lastError"] = self.last_error
        return data


@dataclass
class TerminalDeadLetter:
    payload: Dict[str, Any]
    rejected_at: int
    response_cmd: str
    code: Optional[int] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "payload": _clone_payload(self.payload),
            "rejectedAt": self.rejected_at,
            "responseCmd": self.response_cmd,
        }
        if self.code is not None:
            data["code"] = self.code
        if self.message is not None:
            data["message"] = self.message
        return data


@dataclass(frozen=True)
class TerminalRejection:
    response_cmd: str
    code: Optional[int] = None
    message: Optional[str] = None


class TerminalOutbox:
    """Must durably record a terminal result before the first WebSocket send."""

    def __init__(self, file_path: Optional[str] = None):
        self._file_path = file_path
        self._pending: Dict[str, TerminalOutboxEntry] = {}
        self._dead_letters: List[TerminalDeadLetter] = []
        self._load()

    def enqueue(self, payload: Dict[str, Any]) -> TerminalOutboxEntry:
        if not _is_event_result_payload(payload):
            raise ValueError(
                "terminal outbox requires a non-empty event_id and terminal status"
            )
        event_id = str(payload["event_id"]).strip()
        previous = self._pending.get(event_id)
        now = _now_ms()
        entry = TerminalOutboxEntry(
            payload=_clone_payload({**payload, "event_id": event_id}),
            generation=(previous.generation if previous else 0) + 1,
            created_at=previous.created_at if previous else now,
            updated_at=now,
            attempts=0,
            next_attempt_at=0,
        )
        self._pending[event_id] = entry
        try:
            self._persist()
        except Exception:
            if previous:
                self._pending[event_id] = previous
            else:
                self._pending.pop(event_id, None)
            raise
        return entry.clone()

    def get(self, event_id: str) -> Optional[TerminalOutboxEntry]:
        entry = self._pending.get(event_id)
        return entry.clone() if entry else None

    def list_pending(self) -> List[TerminalOutboxEntry]:
        return [entry.clone() for entry in self._pending.values()]

    def list_dead_letters(self) -> List[TerminalDeadLetter]:
        return [
            TerminalDeadLetter(
                payload=_clone_payload(item.payload),
                rejected_at=item.rejected_at,
                response_cmd=item.response_cmd,
                code=item.code,
                message=item.message,
            )
            for item in self._dead_letters
        ]

    def is_current(self, entry: TerminalOutboxEntry) -> bool:
        current = self._pending.get(str(entry.payload.get("event_id") or ""))
        return (
            current is not None
            and current.generation == entry.generation
            and current.payload.get("status") == entry.payload.get("status")
        )

    def mark_delivery_started(
        self, entry: TerminalOutboxEntry
    ) -> Optional[TerminalOutboxEntry]:
        event_id = str(entry.payload.get("event_id") or "")
        current = self._pending.get(event_id)
        if not current or current.generation != entry.generation:
            return None
        if current.delivery_started_at:
            return current.clone()
        previous = current.clone()
        started = _now_ms()
        current.delivery_started_at = started
        current.updated_at = started
        try:
            self._persist()
        except Exception:
            self._pending[event_id] = previous
            raise
        return current.clone()

    def acknowledge(
        self,
        entry: TerminalOutboxEntry,
        ack_event_id: Optional[str],
        ack_status: Optional[str],
        ack_terminal_commit_token: Optional[str] = None,
        terminal_committed: Optional[bool] = None,
    ) -> bool:
        if (
            ack_event_id != entry.payload.get("event_id")
            or ack_status != entry.payload.get("status")
        ):
            return False
        expected_token = str(entry.payload.get("terminal_commit_token") or "").strip()
        if expected_token and (
            str(ack_terminal_commit_token or "").strip() != expected_token
            or terminal_committed is not True
        ):
            return False
        current = self._pending.get(str(entry.payload.get("event_id") or ""))
        if (
            not current
            or current.generation != entry.generation
            or current.payload.get("status") != entry.payload.get("status")
            or str(current.payload.get("terminal_commit_token") or "").strip()
            != expected_token
        ):
            return False
        event_id = str(entry.payload.get("event_id") or "")
        self._pending.pop(event_id, None)
        try:
            self._persist()
        except Exception:
            self._pending[event_id] = current
            raise
        return True

    def record_retry(
        self, entry: TerminalOutboxEntry, next_attempt_at: int, error: str
    ) -> bool:
        event_id = str(entry.payload.get("event_id") or "")
        current = self._pending.get(event_id)
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
            self._pending[event_id] = previous
            raise
        return True

    def move_to_dead_letter(
        self, entry: TerminalOutboxEntry, rejection: TerminalRejection
    ) -> bool:
        event_id = str(entry.payload.get("event_id") or "")
        current = self._pending.get(event_id)
        if not current or current.generation != entry.generation:
            return False
        dead = TerminalDeadLetter(
            payload=_clone_payload(current.payload),
            rejected_at=_now_ms(),
            response_cmd=rejection.response_cmd,
            code=rejection.code,
            message=rejection.message,
        )
        previous_dead = list(self._dead_letters)
        self._pending.pop(event_id, None)
        self._dead_letters = [*self._dead_letters, dead][-MAX_DEAD_LETTERS:]
        try:
            self._persist()
        except Exception:
            self._pending[event_id] = current
            self._dead_letters = previous_dead
            raise
        return True

    def _load(self) -> None:
        try:
            file = read_json_object(self._file_path)
        except RuntimeError as exc:
            raise RuntimeError(f"terminal outbox load failed: {exc}") from exc
        if file is None:
            return
        if file.get("version") != FILE_VERSION or not isinstance(file.get("pending"), dict):
            raise RuntimeError("terminal outbox load failed: unsupported or invalid file")

        for event_id, raw in file["pending"].items():
            if not isinstance(raw, dict):
                continue
            payload = raw.get("payload")
            if event_id != (payload or {}).get("event_id") or not _is_event_result_payload(
                payload
            ):
                continue
            persisted_started = raw.get("deliveryStartedAt")
            started = (
                int(persisted_started)
                if isinstance(persisted_started, (int, float)) and persisted_started > 0
                else None
            )
            # Legacy output-guard entries may have reached the network before this
            # marker existed. Treat a process-recovered guard as frozen.
            if payload.get("code") == "agent_output_unconfirmed":
                updated_at = raw.get("updatedAt")
                recovered = started
                if recovered is None and isinstance(updated_at, (int, float)):
                    recovered = int(updated_at)
                if recovered is None:
                    recovered = _now_ms()
                started = recovered
            generation = raw.get("generation")
            created_at = raw.get("createdAt")
            updated_at = raw.get("updatedAt")
            attempts = raw.get("attempts")
            next_attempt = raw.get("nextAttemptAt")
            self._pending[event_id] = TerminalOutboxEntry(
                payload=_clone_payload(payload),
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
                    int(attempts)
                    if isinstance(attempts, int) and attempts >= 0
                    else 0
                ),
                next_attempt_at=(
                    int(next_attempt) if isinstance(next_attempt, (int, float)) else 0
                ),
                delivery_started_at=started,
                last_error=(
                    str(raw["lastError"])
                    if isinstance(raw.get("lastError"), str)
                    else None
                ),
            )

        dead_raw = file.get("deadLetters")
        if isinstance(dead_raw, list):
            letters: List[TerminalDeadLetter] = []
            for item in dead_raw:
                if not isinstance(item, dict):
                    continue
                if not _is_event_result_payload(item.get("payload")):
                    continue
                if item.get("responseCmd") not in ("send_nack", "error"):
                    continue
                letters.append(
                    TerminalDeadLetter(
                        payload=_clone_payload(item["payload"]),
                        rejected_at=int(item.get("rejectedAt") or _now_ms()),
                        response_cmd=str(item["responseCmd"]),
                        code=(
                            int(item["code"])
                            if isinstance(item.get("code"), (int, float))
                            else None
                        ),
                        message=(
                            str(item["message"])
                            if isinstance(item.get("message"), str)
                            else None
                        ),
                    )
                )
            self._dead_letters = letters[-MAX_DEAD_LETTERS:]

    def _persist(self) -> None:
        if not self._file_path:
            return
        snapshot = {
            "version": FILE_VERSION,
            "pending": {
                event_id: entry.to_dict() for event_id, entry in self._pending.items()
            },
            "deadLetters": [item.to_dict() for item in self.list_dead_letters()],
        }
        atomic_write_json(self._file_path, snapshot)
