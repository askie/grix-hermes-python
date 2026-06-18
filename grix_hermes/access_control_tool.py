"""grix_access_control tool — manage who may message this agent.

Mirrors grix-connector's typed `grix_access_control` tool. The connector maps a
user-facing action to an internal verb and forwards it over the agent_invoke
channel as the backend action `claude_access_control` with `{verb, payload}`
(see grix-connector/src/adapter/claude/claude-adapter.ts and
docs/adapter-protocol-claude.md). This tool reproduces that contract exactly.
"""

import logging
from typing import Any, Dict

from .contract import CAP_AGENT_INVOKE_V1

logger = logging.getLogger(__name__)

# Backend action recognized by the Grix platform for access-control operations.
_BACKEND_ACTION = "claude_access_control"

# User-facing action → internal invoke verb (matches connector ACCESS_CONTROL_ACTION_MAP).
ACTION_VERB_MAP = {
    "pair_approve": "pair_approve",
    "pair_deny": "pair_deny",
    "allow_sender": "sender_allow",
    "remove_sender": "sender_remove",
    "set_policy": "policy_set",
}

GRIX_ACCESS_CONTROL_SCHEMA = {
    "name": "grix_access_control",
    "description": (
        "Manage who may message this agent. Pick exactly one action:\n"
        "  pair_approve / pair_deny — approve or deny a pairing request (requires `code`)\n"
        "  allow_sender — add a sender to the allowlist (requires `sender_id`)\n"
        "  remove_sender — remove a sender (requires `sender_id`)\n"
        "  set_policy — set the access policy (requires `policy`: allowlist | open | disabled)\n"
        "These actions change who can reach the agent — confirm with the user before "
        "approving an unknown pairing code or switching the policy to `open`."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": list(ACTION_VERB_MAP.keys()),
                "description": "Access control action type.",
            },
            "code": {
                "type": "string",
                "description": "Pairing code (required for pair_approve / pair_deny).",
            },
            "sender_id": {
                "type": "string",
                "description": "Sender ID (required for allow_sender / remove_sender).",
            },
            "policy": {
                "type": "string",
                "enum": ["allowlist", "open", "disabled"],
                "description": "Access policy (required for set_policy).",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Optional request timeout in milliseconds (default: 30000).",
                "default": 30000,
            },
        },
        "required": ["action"],
    },
}


def _check_grix_access_control() -> bool:
    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform

        runner = _gateway_runner_ref()
        if not runner:
            return False
        adapter = runner.adapters.get(Platform("grix"))
        if not adapter:
            return False
        return CAP_AGENT_INVOKE_V1 in (adapter.connection.capabilities or [])
    except Exception:
        return False


def _build_payload(action: str, args: dict) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if action in ("pair_approve", "pair_deny"):
        code = str(args.get("code") or "").strip()
        if not code:
            raise ValueError(f"'{action}' requires a pairing 'code'")
        payload["code"] = code
    elif action in ("allow_sender", "remove_sender"):
        sender_id = str(args.get("sender_id") or "").strip()
        if not sender_id:
            raise ValueError(f"'{action}' requires a 'sender_id'")
        payload["sender_id"] = sender_id
    elif action == "set_policy":
        policy = str(args.get("policy") or "").strip()
        if policy not in ("allowlist", "open", "disabled"):
            raise ValueError("'set_policy' requires 'policy' to be one of: allowlist, open, disabled")
        payload["policy"] = policy
    return payload


async def _grix_access_control_handler(args: dict, **_kwargs) -> str:
    from tools.registry import tool_error, tool_result
    from gateway.config import Platform

    action = (args.get("action") or "").strip()
    verb = ACTION_VERB_MAP.get(action)
    if not verb:
        return tool_error(
            f"Unknown action '{action}'. Supported: {', '.join(sorted(ACTION_VERB_MAP))}"
        )

    try:
        payload = _build_payload(action, args)
    except ValueError as exc:
        return tool_error(str(exc))

    timeout_ms = args.get("timeout_ms")
    if timeout_ms is not None:
        try:
            timeout_ms = int(timeout_ms)
        except (TypeError, ValueError):
            return tool_error("timeout_ms must be an integer")

    try:
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        if not runner:
            return tool_error("Gateway is not running")

        adapter = runner.adapters.get(Platform("grix"))
        if not adapter:
            return tool_error("Grix adapter is not connected")

        result = await adapter.agent_invoke(
            action=_BACKEND_ACTION,
            params={"verb": verb, "payload": payload},
            timeout_ms=timeout_ms or 30_000,
        )
        return tool_result(result)
    except Exception as exc:
        logger.warning("grix_access_control '%s' failed: %s", action, exc)
        return tool_error(f"access_control failed: {exc}")


def register_access_control_tool(ctx=None) -> None:
    _register = ctx.register_tool if ctx else None
    if _register:
        _register(
            name="grix_access_control",
            toolset="grix",
            schema=GRIX_ACCESS_CONTROL_SCHEMA,
            handler=_grix_access_control_handler,
            check_fn=_check_grix_access_control,
            is_async=True,
            description="Manage sender access control: pair approval, allow/remove senders, set policy.",
            emoji="🔐",
        )
    else:
        from tools.registry import registry

        registry.register(
            name="grix_access_control",
            toolset="grix",
            schema=GRIX_ACCESS_CONTROL_SCHEMA,
            handler=_grix_access_control_handler,
            check_fn=_check_grix_access_control,
            is_async=True,
            description="Manage sender access control: pair approval, allow/remove senders, set policy.",
            emoji="🔐",
        )
