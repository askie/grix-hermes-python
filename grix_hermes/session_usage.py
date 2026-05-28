"""Grix get_session_usage local action handler.

Resolves aibot session_id to hermes internal session via sessions.json,
then queries state.db for aggregated token usage.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def handle_session_usage_action(
    params: Dict[str, Any],
    *,
    hermes_home: str,
) -> Dict[str, Any]:
    aibot_session_id = str(params.get("session_id") or "").strip()
    if not aibot_session_id:
        return _fail("missing_session_id", "session_id is required")

    hermes_session_id = _resolve_hermes_session_id(aibot_session_id, hermes_home)
    if not hermes_session_id:
        return _fail("session_not_found", f"No hermes session found for aibot session {aibot_session_id}")

    db_path = os.path.join(hermes_home, "state.db")
    if not os.path.isfile(db_path):
        return _fail("db_not_found", "Hermes state database not found")

    result = _query_session_usage(db_path, hermes_session_id)
    if result is None:
        return _fail("session_not_found", f"Session {hermes_session_id} not found in state database")

    return {
        "status": "ok",
        "result": {
            "sessionId": aibot_session_id,
            "adapterType": "hermes",
            "models": result["models"],
            "total": result["total"],
            "turns": result["turns"],
            "sampledAt": datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        },
    }


def _resolve_hermes_session_id(aibot_session_id: str, hermes_home: str) -> Optional[str]:
    sessions_path = os.path.join(hermes_home, "sessions", "sessions.json")
    if not os.path.isfile(sessions_path):
        return None
    try:
        with open(sessions_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    for _key, entry in data.items():
        origin = entry.get("origin") or {}
        if str(origin.get("chat_id") or "").strip() == aibot_session_id:
            sid = str(entry.get("session_id") or "").strip()
            if sid:
                return sid
    return None


def _query_session_usage(db_path: str, hermes_session_id: str) -> Optional[Dict[str, Any]]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
                "message_count, api_call_count FROM sessions WHERE id = ?",
                (hermes_session_id,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None

    if not row:
        return None

    model = row["model"] or "unknown"
    total = {
        "inputTokens": row["input_tokens"] or 0,
        "outputTokens": row["output_tokens"] or 0,
        "cacheReadInputTokens": row["cache_read_tokens"] or 0,
        "cacheCreationInputTokens": row["cache_write_tokens"] or 0,
    }

    return {
        "models": [
            {
                "model": model,
                "turns": row["api_call_count"] or 0,
                "total": total,
            }
        ],
        "total": total,
        "turns": row["api_call_count"] or 0,
    }


def _fail(error_code: str, error_msg: str) -> Dict[str, str]:
    return {"status": "failed", "error_code": error_code, "error_msg": error_msg}
