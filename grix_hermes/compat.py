"""Compatibility helpers for differing Hermes core versions.

This plugin is loaded into user environments where the Hermes core may lag
behind the version that originally introduced some helper modules. Keep the
plugin loadable by providing tiny fallbacks for optional helpers.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional


@dataclass(frozen=True)
class ApprovalPromptMessage:
    content: str
    biz_card: Optional[Dict[str, Any]] = None
    channel_data: Optional[Dict[str, Any]] = None


try:
    from gateway.platforms.card_actions import build_card_action_user_text as _native_build_card_action_user_text
except Exception:  # pragma: no cover - depends on host Hermes version
    _native_build_card_action_user_text = None


try:
    from gateway.platforms.card_actions import sanitize_card_action_tag as _native_sanitize_card_action_tag
except Exception:  # pragma: no cover - depends on host Hermes version
    _native_sanitize_card_action_tag = None


try:
    from gateway.platforms.hermes_exec_approval import build_exec_approval_message as _native_build_exec_approval_message
except Exception:  # pragma: no cover - depends on host Hermes version
    _native_build_exec_approval_message = None


def sanitize_card_action_tag(tag: Any) -> str:
    if _native_sanitize_card_action_tag is not None:
        return _native_sanitize_card_action_tag(tag)

    raw = str(tag or "").strip().lower().replace(" ", "_")
    if not raw:
        return "button"
    cleaned = "".join(ch if (ch.isalnum() or ch in {"_", "-", "."}) else "_" for ch in raw)
    cleaned = cleaned.strip("._-")
    return cleaned or "button"


def build_card_action_user_text(tag: Any, value: Any) -> str:
    if _native_build_card_action_user_text is not None:
        return _native_build_card_action_user_text(tag, value)

    normalized_tag = sanitize_card_action_tag(tag)
    if value in (None, "", [], {}):
        return f"[Card action: {normalized_tag}]"

    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        rendered = str(value)
    if len(rendered) > 500:
        rendered = rendered[:497] + "..."
    return f"[Card action: {normalized_tag}] {rendered}"


_DEFAULT_APPROVAL_TIMEOUT_SEC = 300
_ALLOWED_DECISIONS = ("allow-once", "allow-always", "deny")


def _compact_text(value: str, limit: int) -> str:
    normalized = " ".join(str(value or "").replace("\r\n", "\n").strip().split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)] + "..."


def _decision_commands(approval_id: str) -> Dict[str, str]:
    return {
        "allow-once": f"/approve {approval_id} allow-once",
        "allow-always": f"/approve {approval_id} allow-always",
        "deny": f"/approve {approval_id} deny",
    }


def build_exec_approval_message(
    *,
    approval_id: str,
    command: str,
    description: str,
    raw_approval_data: Optional[Dict[str, Any]] = None,
) -> ApprovalPromptMessage:
    if _native_build_exec_approval_message is not None:
        return _native_build_exec_approval_message(
            approval_id=approval_id,
            command=command,
            description=description,
            raw_approval_data=raw_approval_data,
        )

    normalized_approval_id = str(approval_id or "").strip()
    normalized_command = str(command or "").replace("\r\n", "\n").strip()
    normalized_description = str(description or "").replace("\r\n", "\n").strip()

    decisions = list(_ALLOWED_DECISIONS)
    decision_commands = _decision_commands(normalized_approval_id)

    raw_payload: Dict[str, Any] = (
        copy.deepcopy(dict(raw_approval_data)) if isinstance(raw_approval_data, Mapping) else {}
    )
    raw_payload["approval_id"] = normalized_approval_id
    raw_payload["command"] = normalized_command
    raw_payload["description"] = normalized_description
    raw_payload["host"] = "hermes"
    raw_payload["expires_in_seconds"] = _DEFAULT_APPROVAL_TIMEOUT_SEC
    raw_payload["allowed_decisions"] = list(decisions)
    raw_payload["decision_commands"] = dict(decision_commands)

    biz_payload: Dict[str, Any] = {
        "approval_id": normalized_approval_id,
        "approval_slug": normalized_approval_id,
        "approval_command_id": normalized_approval_id,
        "command": normalized_command,
        "host": "hermes",
        "allowed_decisions": list(decisions),
        "decision_commands": dict(decision_commands),
        "expires_in_seconds": _DEFAULT_APPROVAL_TIMEOUT_SEC,
    }
    if normalized_description:
        biz_payload["warning_text"] = normalized_description

    fallback_lines = [
        f"[Exec Approval] {_compact_text(normalized_command, 160)} (hermes)",
        decision_commands["allow-once"],
    ]
    if normalized_description:
        fallback_lines.append(f"Reason: {normalized_description}")

    return ApprovalPromptMessage(
        content="\n".join(line for line in fallback_lines if line),
        biz_card={
            "version": 1,
            "type": "exec_approval",
            "payload": biz_payload,
        },
        channel_data={
            "hermes": {
                "execApprovalPending": raw_payload,
            }
        },
    )
