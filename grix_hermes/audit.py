"""Small, provider-neutral audit replay implementation for the Hermes port.

The connector owns provider-specific evidence adapters.  Hermes does not expose
those provider logs, but it can still provide a truthful replay of the Grix
turn boundary and the text exchanged through this adapter.  The module keeps
the wire contract compatible with the connector without claiming unavailable
tool/token evidence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .contract import AUDIT_LOCAL_ACTION_TYPES

AUDIT_LOCAL_ACTIONS = AUDIT_LOCAL_ACTION_TYPES
_CURSOR_VERSION = 1
_CURSOR_SECRET = secrets.token_bytes(32)

# Hermes 只能如实回放 adapter 自己观察到的回合边界与收发文本，没有 provider
# 日志/原始请求体/用量上报。内容可信度按 connector 语义标记为 reconstructed，
# 缺失证据用 connector CaptureGap 的枚举原因说明，不虚构。
CAPTURE_LEVEL = "reconstructed"
PROVENANCE = {"source": "reconstructed", "accuracy": "estimated"}

# event_result 状态 → connector 回放 outcome/span status 枚举。
_OUTCOME_MAP = {
    "responded": "completed",
    "failed": "failed",
    "canceled": "cancelled",
}


def normalize_outcome(status: str) -> str:
    return _OUTCOME_MAP.get(status, "failed")


def _iso(ms: int) -> str:
    """Millisecond epoch → connector 风格的 ISO-8601（毫秒精度、Z 后缀）。"""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

AUDIT_ERROR_CODES = {
    "invalid_params": "AUDIT_INVALID_PARAMS",
    "not_found": "AUDIT_NOT_FOUND",
    "revision_not_found": "AUDIT_REVISION_NOT_FOUND",
    "cursor_invalid": "AUDIT_CURSOR_INVALID",
    "content_forbidden": "AUDIT_CONTENT_FORBIDDEN",
    "content_not_found": "AUDIT_CONTENT_NOT_FOUND",
    "content_corrupt": "AUDIT_CONTENT_CORRUPT",
    "internal": "AUDIT_INTERNAL",
}


def new_audit_id() -> str:
    return f"audit-{uuid.uuid4().hex}"


def new_turn_id() -> str:
    return f"turn-{uuid.uuid4().hex}"


def _content_ref(content_id: str, role: str, value: str) -> Dict[str, Any]:
    encoded = value.encode("utf-8")
    return {
        "content_id": content_id,
        "kind": role,
        "mime": "text/plain; charset=utf-8",
        "bytes": len(encoded),
        "chars": len(value),
        "estimated_tokens": max(1, math.ceil(len(encoded) / 4)) if encoded else 0,
        "capture_level": CAPTURE_LEVEL,
    }


def _record(value: Any) -> Optional[Dict[str, Any]]:
    return value if isinstance(value, dict) else None


def _pick(value: Dict[str, Any], camel: str, snake: str) -> Any:
    camel_value = value.get(camel)
    return value.get(snake) if camel_value is None else camel_value


def _bool(value: Any, field: str, fallback: bool) -> tuple[Optional[bool], Optional[str]]:
    if value is None:
        return fallback, None
    if not isinstance(value, bool):
        return None, f"audit.{field} must be a boolean"
    return value, None


def parse_audit_options(extra: Any) -> Dict[str, Any]:
    """Parse the connector audit option shape, including nested ``extra``."""
    raw = _record(extra)
    if raw is None:
        return {"state": "absent"}
    nested = _record(raw.get("extra"))
    audit = raw.get("audit")
    if audit is None and nested:
        audit = nested.get("audit")
    if audit is None:
        return {"state": "absent"}
    audit = _record(audit)
    if audit is None:
        return {"error": "audit must be an object"}
    if not isinstance(audit.get("enabled"), bool):
        return {"error": "audit.enabled must be a boolean"}
    if not audit["enabled"]:
        return {"state": "disabled", "options": {"enabled": False}}

    scope = audit.get("scope", "session")
    if scope not in ("session", "turn"):
        return {"error": 'audit.scope must be "session" or "turn"'}

    # Hermes has no provider-level setup boundary.  Turn scope therefore uses
    # the same safe text-only replay profile as session scope.
    profile = audit.get("profile", "replay")
    if profile not in ("usage", "replay", "full"):
        return {"error": "audit.profile must be usage, replay, or full"}
    defaults = {
        "input_output": profile != "usage",
        "tools": profile != "usage",
        "subagents": True,
        "thinking": False,
        "raw_provider_body": False,
    }
    raw_capture = audit.get("capture")
    if raw_capture is not None and _record(raw_capture) is None:
        return {"error": "audit.capture must be an object"}
    capture = _record(raw_capture) or {}
    for camel, snake in (
        ("inputOutput", "input_output"),
        ("tools", "tools"),
        ("subagents", "subagents"),
        ("thinking", "thinking"),
        ("rawProviderBody", "raw_provider_body"),
    ):
        value, error = _bool(_pick(capture, camel, snake), snake, defaults[snake])
        if error:
            return {"error": error}
        # Provider bodies and thinking are not available from Hermes and must
        # never be advertised as captured just because a client requested them.
        if snake in ("thinking", "raw_provider_body"):
            value = False
        defaults[snake] = bool(value)

    retention = _pick(audit, "retentionDays", "retention_days")
    if retention is not None and (
        not isinstance(retention, int) or isinstance(retention, bool) or not 1 <= retention <= 365
    ):
        return {"error": "audit.retention_days must be an integer between 1 and 365"}
    options = {
        "enabled": True,
        "scope": scope,
        "profile": profile,
        "capture": defaults,
    }
    if retention is not None:
        options["retention_days"] = retention
    return {"state": "enabled", "options": options}


def _token(value: Dict[str, Any]) -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps({"v": _CURSOR_VERSION, **value}, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encoded}.{_cursor_signature(encoded)}"


def _cursor_signature(encoded: str) -> str:
    signature = hmac.new(_CURSOR_SECRET, encoded.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")


def _untoken(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded, signature = value.split(".")
        if not hmac.compare_digest(signature, _cursor_signature(encoded)):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        result = json.loads(base64.urlsafe_b64decode(padded).decode())
        if result.get("v") != _CURSOR_VERSION:
            return None
        return result if isinstance(result, dict) else None
    except Exception:
        return None


class AuditReplayError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HermesAuditStore:
    """0600 JSON replay files with immutable content blobs under a 0700 root."""

    def __init__(self, root_dir: Optional[str] = None):
        configured = root_dir or os.environ.get("GRIX_AUDIT_STORAGE_ROOT")
        if configured:
            self.root = Path(configured).expanduser()
        else:
            home = Path(os.environ.get("GRIX_CONNECTOR_HOME", "~/.grix")).expanduser()
            self.root = home / "audit-replay"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass

    def _path(self, audit_id: str, turn_id: str) -> Path:
        audit_key = hashlib.sha256(audit_id.encode("utf-8")).hexdigest()
        turn_key = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        return self.root / "replays" / audit_key / f"{turn_key}.json"

    def save(self, replay: Dict[str, Any]) -> None:
        path = self._path(replay["audit_id"], replay["turn_id"])
        replay_root = self.root / "replays"
        replay_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(replay_root, 0o700)
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temp.write_text(json.dumps(replay, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)

    def _load(self, audit_id: str, turn_id: str, revision: Optional[int] = None) -> Dict[str, Any]:
        path = self._path(audit_id, turn_id)
        if not path.is_file():
            raise AuditReplayError(AUDIT_ERROR_CODES["not_found"], "Audit turn was not found")
        try:
            replay = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AuditReplayError(AUDIT_ERROR_CODES["content_corrupt"], "Audit replay is corrupt") from exc
        if revision is not None and replay.get("revision") != revision:
            raise AuditReplayError(AUDIT_ERROR_CODES["revision_not_found"], "Audit turn revision was not found")
        return replay

    @staticmethod
    def _coordinates(params: Any) -> Dict[str, Any]:
        record = _record(params)
        if record is None:
            raise AuditReplayError(AUDIT_ERROR_CODES["invalid_params"], "Audit parameters must be an object")
        result: Dict[str, Any] = {}
        for field in ("session_id", "audit_id", "turn_id"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 512:
                raise AuditReplayError(AUDIT_ERROR_CODES["invalid_params"], f"{field} is required")
            result[field] = value.strip()
        revision = record.get("revision")
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool) or revision < 1):
            raise AuditReplayError(AUDIT_ERROR_CODES["invalid_params"], "revision is invalid")
        if revision is not None:
            result["revision"] = revision
        return result

    def action(self, action_type: str, params: Any) -> Dict[str, Any]:
        try:
            coordinates = self._coordinates(params)
            replay = self._load(coordinates["audit_id"], coordinates["turn_id"], coordinates.get("revision"))
            if replay.get("session_id") != coordinates["session_id"]:
                raise AuditReplayError(AUDIT_ERROR_CODES["not_found"], "Audit turn was not found for this session")
            if action_type == "audit_get_manifest":
                return {"status": "ok", "result": replay["manifest"]}
            if action_type == "audit_list_spans":
                return self._paged(replay, params, "spans", coordinates)
            if action_type == "audit_get_content_chunk":
                return self._content_chunk(replay, params, coordinates)
            raise AuditReplayError(AUDIT_ERROR_CODES["invalid_params"], "Unsupported audit local action")
        except AuditReplayError as exc:
            return {"status": "failed", "error_code": exc.code, "error_msg": str(exc)}
        except Exception:
            return {"status": "failed", "error_code": AUDIT_ERROR_CODES["internal"], "error_msg": "Audit action failed"}

    @staticmethod
    def _paged(
        replay: Dict[str, Any],
        params: Any,
        kind: str,
        coordinates: Dict[str, Any],
    ) -> Dict[str, Any]:
        record = _record(params) or {}
        limit = record.get("limit", 100)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise AuditReplayError(AUDIT_ERROR_CODES["invalid_params"], "limit is invalid")
        cursor = _untoken(record.get("cursor"))
        if record.get("cursor") not in (None, "") and (
            cursor is None
            or cursor.get("kind") != kind
            or cursor.get("auditId") != coordinates["audit_id"]
            or cursor.get("turnId") != coordinates["turn_id"]
            or cursor.get("revision") != replay["revision"]
        ):
            raise AuditReplayError(AUDIT_ERROR_CODES["cursor_invalid"], "Audit cursor is invalid")
        position = int(cursor.get("position", 0)) if cursor else 0
        if position < 0 or position > len(replay.get(kind, [])):
            raise AuditReplayError(AUDIT_ERROR_CODES["cursor_invalid"], "Audit cursor is out of range")
        items = replay.get(kind, [])[position : position + limit]
        next_position = position + len(items)
        more = next_position < len(replay.get(kind, []))
        return {
            "status": "ok",
            "result": {
                "items": items,
                "next_cursor": _token({
                    "kind": kind,
                    "position": next_position,
                    "auditId": coordinates["audit_id"],
                    "turnId": coordinates["turn_id"],
                    "revision": replay["revision"],
                }) if more else None,
                "has_more": more,
            },
        }

    @staticmethod
    def _content_chunk(
        replay: Dict[str, Any],
        params: Any,
        coordinates: Dict[str, Any],
    ) -> Dict[str, Any]:
        record = _record(params) or {}
        content_id = record.get("content_id")
        refs = replay.get("content_refs", {})
        if not isinstance(content_id, str) or content_id not in refs:
            raise AuditReplayError(AUDIT_ERROR_CODES["content_forbidden"], "Requested content does not belong to this audit turn")
        max_bytes = record.get("max_bytes", 131072)
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 4 <= max_bytes <= 131072:
            raise AuditReplayError(AUDIT_ERROR_CODES["invalid_params"], "max_bytes is invalid")
        raw = refs[content_id].encode("utf-8")
        if hashlib.sha256(raw).hexdigest() != content_id:
            raise AuditReplayError(
                AUDIT_ERROR_CODES["content_corrupt"],
                "Audit content hash does not match its manifest",
            )
        cursor = _untoken(record.get("cursor"))
        if record.get("cursor") not in (None, "") and (
            cursor is None
            or cursor.get("kind") != "content"
            or cursor.get("auditId") != coordinates["audit_id"]
            or cursor.get("turnId") != coordinates["turn_id"]
            or cursor.get("revision") != replay["revision"]
            or cursor.get("contentId") != content_id
            or cursor.get("etag") != content_id
        ):
            raise AuditReplayError(AUDIT_ERROR_CODES["cursor_invalid"], "Audit cursor is invalid")
        start = int(cursor.get("position", 0)) if cursor else 0
        if start < 0 or start > len(raw):
            raise AuditReplayError(AUDIT_ERROR_CODES["cursor_invalid"], "Audit cursor is out of range")
        chunk = raw[start : start + max_bytes]
        while chunk:
            try:
                value = chunk.decode("utf-8")
                break
            except UnicodeDecodeError as exc:
                if exc.end != len(chunk):
                    raise AuditReplayError(
                        AUDIT_ERROR_CODES["content_corrupt"],
                        "Audit content is not valid UTF-8",
                    ) from exc
                chunk = chunk[:exc.start]
        else:
            value = ""
        end = start + len(chunk)
        eof = end >= len(raw)
        return {
            "status": "ok",
            "result": {
                "content_id": content_id,
                "mime": "text/plain; charset=utf-8",
                "encoding": "utf-8",
                "value": value,
                "byte_start": start,
                "byte_end": end,
                "total_bytes": len(raw),
                "next_cursor": None if eof else _token({
                    "kind": "content",
                    "position": end,
                    "auditId": coordinates["audit_id"],
                    "turnId": coordinates["turn_id"],
                    "revision": replay["revision"],
                    "contentId": content_id,
                    "etag": content_id,
                }),
                "eof": eof,
                "etag": content_id,
                "revision": replay["revision"],
            },
        }

    def finalize(
        self,
        *,
        audit_id: str,
        turn_id: str,
        session_id: str,
        event_id: str,
        msg_id: Optional[str],
        provider: str,
        started_at: int,
        input_text: str,
        output_text: str,
        outcome: str,
    ) -> Dict[str, Any]:
        now = int(time.time() * 1000)
        outcome = normalize_outcome(outcome)
        refs = {}
        input_id = hashlib.sha256(input_text.encode("utf-8")).hexdigest() if input_text else None
        output_id = hashlib.sha256(output_text.encode("utf-8")).hexdigest() if output_text else None
        if input_text:
            refs[input_id] = input_text
        if output_text:
            refs[output_id] = output_text
        input_ref = _content_ref(input_id, "user_input", input_text) if input_text else None
        output_ref = _content_ref(output_id, "final_response", output_text) if output_text else None
        spans = [{
            "span_id": f"{turn_id}:turn",
            "trace_id": audit_id,
            "turn_id": turn_id,
            "session_id": session_id,
            "kind": "turn",
            "name": "Hermes audited turn",
            "sequence": 0,
            "status": outcome,
            "started_at": _iso(started_at),
            "ended_at": _iso(now),
            "duration_ms": max(0, now - started_at),
            "input_refs": [input_ref] if input_ref else [],
            "output_refs": [output_ref] if output_ref else [],
            "provider": {"provider": provider, "native_ids": {}},
            "provenance": dict(PROVENANCE),
        }]
        # 缺失证据必须显式标注，原因枚举与 connector CaptureGap 对齐。
        gaps = [
            {"category": "tool_calls", "reason": "source_log_missing"},
            {"category": "subagents", "reason": "source_log_missing"},
            {"category": "usage", "reason": "provider_usage_missing"},
            {"category": "raw_provider_body", "reason": "raw_capture_disabled"},
        ]
        raw_api_capture = {
            "requested": False,
            "available": False,
            "status": "not_requested",
            "stored_ref_count": 0,
            "exchange_count": 0,
            "stored_request_count": 0,
            "stored_response_count": 0,
            "omitted_count": 0,
        }
        quality = {
            "status": "partial",
            "input_complete": bool(input_text),
            "output_complete": bool(output_text),
            "tool_calls_complete": False,
            "subagents_complete": False,
            "usage_complete": False,
            "raw_requests_complete": False,
            "raw_requests_requested": False,
            "raw_requests_available": False,
            "raw_requests_status": "not_requested",
            "raw_request_capture": raw_api_capture,
            "gaps": gaps,
        }
        statistics = {
            "span_count": 1,
            "span_counts": [{"kind": "turn", "name": "Hermes audited turn", "count": 1}],
            "llm_request_count": 0,
            "tool_call_count": 0,
            "tool_calls_complete_count": 0,
            "tool_call_status_counts": {},
            "tool_calls_returned_count": 0,
            "tool_calls_has_more": False,
            "subagent_call_count": 0,
            "llm_requests": [],
            "tool_calls": [],
            "input_token_attribution": {
                "accuracy": "estimated_from_captured_content_refs",
                "provider_reported_input_tokens": 0,
                "captured_input_ref_count": 1 if input_ref else 0,
                "captured_input_bytes": input_ref["bytes"] if input_ref else 0,
                "captured_input_estimated_tokens": input_ref["estimated_tokens"] if input_ref else 0,
                "token_estimation_method": "utf8_bytes_divided_by_4",
                "note": "Provider usage is unavailable to the Hermes adapter; only the turn "
                        "boundary and adapter-observed text are replayed.",
            },
            "total_usage": {
                "input": {"total": 0, "uncached": None, "cache_read": None, "cache_write": None, "other": None},
                "output": {"total": 0, "reasoning": None, "visible": None},
                "total_processed": 0,
                "request_count": 0,
                "accuracy": "estimated",
                "completeness": "partial",
                "provider": provider,
                "normalization": {"adapter": "hermes", "version": 1, "formula": "provider usage unavailable"},
            },
        }
        replay = {
            "audit_id": audit_id,
            "turn_id": turn_id,
            "revision": 1,
            "session_id": session_id,
            "provider": provider,
            "outcome": outcome,
            "started_at": _iso(started_at),
            "finalized_at": _iso(now),
            "quality": quality,
            "statistics": statistics,
            "spans": spans,
            "content_refs": refs,
            "manifest": {
                "audit_id": audit_id,
                "turn_id": turn_id,
                "revision": 1,
                "session_id": session_id,
                "provider": provider,
                "status": outcome,
                "started_at": _iso(started_at),
                "completed_at": _iso(now),
                "quality": quality,
                "raw_api_capture": raw_api_capture,
                "truncated": False,
                "statistics": statistics,
                "has_spans": True,
                "content_refs": [
                    _content_ref(key, "user_input" if key == input_id else "final_response", value)
                    for key, value in refs.items()
                ],
            },
        }
        self.save(replay)
        return replay
