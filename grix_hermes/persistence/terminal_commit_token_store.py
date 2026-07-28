"""Crash-safe event → terminal commit token registry."""

from __future__ import annotations

from typing import Dict, Optional

from .atomic_json import atomic_unlink, atomic_write_json, read_json_object

FILE_VERSION = 1


class TerminalCommitTokenStore:
    """Persist tokens before inbound events are exposed to bridge code."""

    def __init__(self, file_path: Optional[str] = None):
        self._file_path = file_path
        self._tokens: Dict[str, str] = {}
        self._load()

    def get(self, event_id: str) -> Optional[str]:
        return self._tokens.get(event_id)

    def register(self, event_id: str, token: str) -> None:
        normalized_event_id = event_id.strip()
        normalized_token = token.strip()
        if not normalized_event_id or not normalized_token:
            raise ValueError("terminal commit token requires non-empty event_id and token")
        previous = self._tokens.get(normalized_event_id)
        if previous == normalized_token:
            return
        if previous:
            raise ValueError(f"terminal commit token changed for event={normalized_event_id}")
        self._tokens[normalized_event_id] = normalized_token
        try:
            self._persist()
        except Exception:
            self._tokens.pop(normalized_event_id, None)
            raise

    def remove(self, event_id: str, expected_token: str) -> bool:
        current = self._tokens.get(event_id)
        if not current or current != expected_token:
            return False
        self._tokens.pop(event_id, None)
        try:
            self._persist()
        except Exception:
            self._tokens[event_id] = current
            raise
        return True

    def _load(self) -> None:
        try:
            file = read_json_object(self._file_path)
        except RuntimeError as exc:
            raise RuntimeError(f"terminal commit token store load failed: {exc}") from exc
        if file is None:
            return
        if file.get("version") != FILE_VERSION or not isinstance(file.get("tokens"), dict):
            raise RuntimeError(
                "terminal commit token store load failed: unsupported or invalid file"
            )
        for event_id, token in file["tokens"].items():
            if str(event_id).strip() and isinstance(token, str) and token.strip():
                self._tokens[str(event_id)] = token

    def _persist(self) -> None:
        if not self._file_path:
            return
        if not self._tokens:
            atomic_unlink(self._file_path)
            return
        atomic_write_json(
            self._file_path,
            {"version": FILE_VERSION, "tokens": dict(self._tokens)},
        )
