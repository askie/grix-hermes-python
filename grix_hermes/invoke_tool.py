"""Grix agent_invoke tool for Hermes Agent."""

import logging
from typing import Any, Dict

from .contract import CAP_AGENT_INVOKE_V1

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = {
    "send_msg": "Send a message to a session",
    "delete_msg": "Delete (unsend/recall) a message",
    "contact_search": "Search contacts by keyword or ID",
    "session_search": "Search sessions by keyword",
    "message_history": "Get message history for a session",
    "message_search": "Search messages by keyword in a session",
    "group_create": "Create a new group",
    "group_detail_read": "Read group details",
    "group_leave_self": "Leave a group",
    "group_member_add": "Add members to a group",
    "group_member_remove": "Remove members from a group",
    "group_member_role_update": "Update a group member's role",
    "group_all_members_muted_update": "Toggle mute-all-members for a group",
    "group_member_speaking_update": "Toggle speaking permission for a member",
    "group_dissolve": "Dissolve a group",
    "agent_api_create": "Create a new agent",
    "agent_category_list": "List agent categories",
    "agent_category_create": "Create an agent category",
    "agent_category_update": "Update an agent category",
    "agent_category_assign": "Assign an agent to a category",
    "agent_api_key_rotate": "Rotate an agent's API key",
    "agent_introduction_update": "Update an agent's text introduction",
    "dispatch_agent": "Dispatch one of the owner's agents to work in a directory",
    "call_owner": "Call the owner into a session for a voice talk/approval",
    "session_send": "Send a message into a session AS THE OWNER (owner relay)",
    "chat_state_query": "Query chat-level task states across the owner's sessions (supports session_id/page/page_size/state filtering)",
    "chat_state_update": "Manually update the task state of a specific chat session (params: session_id, state, reason[optional])",
    "egg_search": "Search the egg marketplace (params: keyword, category_id, locale, page, page_size)",
    "egg_get": "Get egg details by ID (params: id[required], locale, version)",
}

GRIX_INVOKE_SCHEMA = {
    "name": "grix_invoke",
    "description": (
        "Unified Grix API — all operations go through the agent_invoke channel.\n\n"
        "Supported actions:\n"
        "  Message: send_msg, delete_msg\n"
        "  Query: contact_search, session_search, message_history, message_search\n"
        "  Group: group_create, group_detail_read, group_leave_self, group_member_add, "
        "group_member_remove, group_member_role_update, group_all_members_muted_update, "
        "group_member_speaking_update, group_dissolve\n"
        "  Admin: agent_api_create, agent_category_list, agent_category_create, "
        "agent_category_update, agent_category_assign, agent_api_key_rotate\n"
        "  Agent dispatch: dispatch_agent, agent_introduction_update\n"
        "  Owner relay: call_owner, session_send\n"
        "  Chat state: chat_state_query, chat_state_update\n"
        "  Egg marketplace: egg_search, egg_get"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "The backend action to invoke.",
                "enum": list(SUPPORTED_ACTIONS.keys()),
            },
            "params": {
                "type": "object",
                "description": "Action-specific parameters as a JSON object.",
            },
            "timeout_ms": {
                "type": "integer",
                "description": "Optional request timeout in milliseconds (default: 15000).",
                "default": 15000,
            },
        },
        "required": ["action"],
    },
}


def _check_grix_invoke() -> bool:
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


async def _grix_invoke_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result
    from gateway.config import Platform

    action = (args.get("action") or "").strip()
    if not action:
        return tool_error("action is required")
    if action not in SUPPORTED_ACTIONS:
        return tool_error(f"Unknown action '{action}'. Supported: {', '.join(sorted(SUPPORTED_ACTIONS))}")

    params = args.get("params")
    if params is not None and not isinstance(params, dict):
        return tool_error("params must be a JSON object")

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
            action=action,
            params=params,
            timeout_ms=timeout_ms,
        )
        return tool_result(result)
    except Exception as exc:
        logger.warning("grix_invoke '%s' failed: %s", action, exc)
        return tool_error(f"agent_invoke failed: {exc}")


def register_invoke_tool(ctx=None) -> None:
    _register = ctx.register_tool if ctx else None
    if _register:
        _register(
            name="grix_invoke",
            toolset="grix",
            schema=GRIX_INVOKE_SCHEMA,
            handler=_grix_invoke_handler,
            check_fn=_check_grix_invoke,
            is_async=True,
            description="Unified Grix API: send/delete messages, query contacts, manage groups, admin agents.",
            emoji="🔗",
        )
    else:
        from tools.registry import registry

        registry.register(
            name="grix_invoke",
            toolset="grix",
            schema=GRIX_INVOKE_SCHEMA,
            handler=_grix_invoke_handler,
            check_fn=_check_grix_invoke,
            is_async=True,
            description="Unified Grix API: send/delete messages, query contacts, manage groups, admin agents.",
            emoji="🔗",
        )
