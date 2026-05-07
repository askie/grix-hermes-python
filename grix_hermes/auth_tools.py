"""Grix auth and agent-management tools for Hermes Agent.

Registers HTTP-based auth operations (send-code, register, login) and
agent management (create, rotate-key) as hermes tools that can be
invoked through the tool registry alongside the WS-based grix_invoke.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict

from . import http_client

logger = logging.getLogger(__name__)

GRIX_AUTH_SCHEMA = {
    "name": "grix_auth",
    "description": (
        "Grix HTTP auth and agent management operations.\n\n"
        "Supported actions:\n"
        "  send_email_code — send a verification code to an email\n"
        "  register — create a new account with email, password, and code\n"
        "  login — login with account and password\n"
        "  list_agents — list all agents for the logged-in user\n"
        "  create_agent — create a new API agent\n"
        "  rotate_api_key — rotate an agent's API key\n"
        "  create_or_reuse_agent — reuse existing agent or create new one"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The auth action to perform.",
                "enum": [
                    "send_email_code",
                    "register",
                    "login",
                    "list_agents",
                    "create_agent",
                    "rotate_api_key",
                    "create_or_reuse_agent",
                ],
            },
            "params": {
                "type": "object",
                "description": "Action-specific parameters.",
            },
        },
        "required": ["action"],
    },
}

GRIX_CARD_SCHEMA = {
    "name": "grix_card",
    "description": (
        "Generate Grix deep-link cards for conversations, user profiles, and egg install status.\n\n"
        "Supported kinds: conversation, user-profile, egg-status"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "description": "Card type to generate.",
                "enum": ["conversation", "user-profile", "egg-status"],
            },
            "params": {
                "type": "object",
                "description": "Card-specific parameters (session_id, title, user_id, etc.).",
            },
        },
        "required": ["kind", "params"],
    },
}


def _require(params: Dict[str, Any], key: str) -> str:
    val = str(params.get(key, "")).strip()
    if not val:
        raise ValueError(f"Missing required parameter: {key}")
    return val


async def _grix_auth_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result

    action = (args.get("action") or "").strip()
    if not action:
        return tool_error("action is required")

    params = args.get("params") or {}
    if not isinstance(params, dict):
        return tool_error("params must be a JSON object")

    base_url = params.get("base_url")

    try:
        if action == "send_email_code":
            result = await http_client.send_email_code(
                email=_require(params, "email"),
                scene=_require(params, "scene"),
                base_url=base_url,
            )
        elif action == "register":
            result = await http_client.register(
                email=_require(params, "email"),
                password=_require(params, "password"),
                email_code=_require(params, "email_code"),
                base_url=base_url,
            )
        elif action == "login":
            result = await http_client.login(
                account=_require(params, "account"),
                password=_require(params, "password"),
                base_url=base_url,
            )
        elif action == "list_agents":
            result = await http_client.list_agents(
                access_token=_require(params, "access_token"),
                base_url=base_url,
            )
            return tool_result(result)
        elif action == "create_agent":
            result = await http_client.create_api_agent(
                access_token=_require(params, "access_token"),
                agent_name=_require(params, "agent_name"),
                is_main=bool(params.get("is_main", True)),
                avatar_url=params.get("avatar_url"),
                base_url=base_url,
            )
        elif action == "rotate_api_key":
            result = await http_client.rotate_api_key(
                access_token=_require(params, "access_token"),
                agent_id=_require(params, "agent_id"),
                is_main=bool(params.get("is_main", True)),
                base_url=base_url,
            )
        elif action == "create_or_reuse_agent":
            result = await http_client.create_or_reuse_agent(
                access_token=_require(params, "access_token"),
                agent_name=_require(params, "agent_name"),
                is_main=bool(params.get("is_main", True)),
                prefer_existing=bool(params.get("prefer_existing", True)),
                rotate_on_reuse=bool(params.get("rotate_on_reuse", True)),
                base_url=base_url,
            )
        else:
            return tool_error(f"Unknown action '{action}'")

        return tool_result(result)
    except (ValueError, http_client.GrixHttpError) as exc:
        return tool_error(str(exc))
    except Exception as exc:
        logger.warning("grix_auth '%s' failed: %s", action, exc)
        return tool_error(f"auth action failed: {exc}")


async def _grix_card_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result

    from .card_links import dispatch_card_builder

    kind = (args.get("kind") or "").strip()
    if not kind:
        return tool_error("kind is required")

    params = args.get("params") or {}
    if not isinstance(params, dict):
        return tool_error("params must be a JSON object")

    try:
        result = dispatch_card_builder(kind, params)
        return tool_result({"ok": True, "kind": kind, "markdown": result})
    except (ValueError, TypeError) as exc:
        return tool_error(str(exc))


def register_auth_tools() -> None:
    from tools.registry import registry

    registry.register(
        name="grix_auth",
        toolset="grix",
        schema=GRIX_AUTH_SCHEMA,
        handler=_grix_auth_handler,
        check_fn=lambda: True,
        is_async=True,
        description="Grix HTTP auth: send email code, register, login, create/rotate agent API keys.",
        emoji="🔑",
    )

    registry.register(
        name="grix_card",
        toolset="grix",
        schema=GRIX_CARD_SCHEMA,
        handler=_grix_card_handler,
        check_fn=lambda: True,
        is_async=True,
        description="Generate Grix deep-link cards for conversations, profiles, and install status.",
        emoji="🃏",
    )
