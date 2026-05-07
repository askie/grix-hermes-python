"""Compatibility helpers for differing Hermes core versions.

This plugin is loaded into user environments where the Hermes core may lag
behind the version that originally introduced some helper modules. Keep the
plugin loadable by providing tiny fallbacks for optional helpers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional


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

    cmd_preview = command[:3000] + "..." if len(command) > 3000 else command
    text = (
        "⚠️ Command Approval Required\n\n"
        f"```\n{cmd_preview}\n```\n\n"
        f"Reason: {description}\n\n"
        "Reply `/approve`, `/approve session`, `/approve always`, or `/deny`."
    )
    return ApprovalPromptMessage(content=text)
