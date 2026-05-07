"""Canonical definitions for the standard AIBOT protocol.

This module is the single source of truth for public AIBOT command names,
status values, error codes, capabilities, and the frozen v1 baseline.
"""

from __future__ import annotations

from typing import Any, Dict

AIBOT_PROTOCOL_VERSION = "aibot-agent-api-v1"
AIBOT_DEFAULT_CONTRACT_VERSION = 1

# Packet commands
CMD_AUTH = "auth"
CMD_AUTH_ACK = "auth_ack"
CMD_PING = "ping"
CMD_PONG = "pong"
CMD_SEND_MSG = "send_msg"
CMD_SEND_ACK = "send_ack"
CMD_SEND_NACK = "send_nack"
CMD_ERROR = "error"
CMD_EDIT_MSG = "edit_msg"
CMD_DELETE_MSG = "delete_msg"
CMD_SESSION_ACTIVITY_SET = "session_activity_set"
CMD_LOCAL_ACTION = "local_action"
CMD_LOCAL_ACTION_RESULT = "local_action_result"
CMD_EVENT_MSG = "event_msg"
CMD_EVENT_ACK = "event_ack"
CMD_EVENT_RESULT = "event_result"
CMD_EVENT_STOP = "event_stop"
CMD_EVENT_STOP_ACK = "event_stop_ack"
CMD_EVENT_STOP_RESULT = "event_stop_result"
CMD_EVENT_EDIT = "event_edit"
CMD_EVENT_REVOKE = "event_revoke"
CMD_SESSION_ROUTE_BIND = "session_route_bind"
CMD_SESSION_ROUTE_RESOLVE = "session_route_resolve"
CMD_AGENT_INVOKE = "agent_invoke"
CMD_AGENT_INVOKE_RESULT = "agent_invoke_result"

STABLE_PUBLIC_COMMANDS = (
    {"cmd": CMD_AUTH, "direction": "client_to_server", "purpose": "authenticate"},
    {"cmd": CMD_AUTH_ACK, "direction": "server_to_client", "purpose": "authentication_result"},
    {"cmd": CMD_PING, "direction": "bidirectional", "purpose": "keepalive_request"},
    {"cmd": CMD_PONG, "direction": "bidirectional", "purpose": "keepalive_response"},
    {"cmd": CMD_EVENT_MSG, "direction": "server_to_client", "purpose": "message_event"},
    {"cmd": CMD_EVENT_ACK, "direction": "client_to_server", "purpose": "message_event_received"},
    {"cmd": CMD_EVENT_RESULT, "direction": "client_to_server", "purpose": "message_event_completed"},
    {"cmd": CMD_EVENT_STOP, "direction": "server_to_client", "purpose": "stop_event"},
    {"cmd": CMD_EVENT_STOP_ACK, "direction": "client_to_server", "purpose": "stop_event_received"},
    {"cmd": CMD_EVENT_STOP_RESULT, "direction": "client_to_server", "purpose": "stop_event_completed"},
    {"cmd": CMD_EVENT_EDIT, "direction": "server_to_client", "purpose": "message_edit_event"},
    {"cmd": CMD_EVENT_REVOKE, "direction": "server_to_client", "purpose": "message_revoke_event"},
    {"cmd": CMD_SEND_MSG, "direction": "client_to_server", "purpose": "send_message"},
    {"cmd": CMD_SEND_ACK, "direction": "server_to_client", "purpose": "send_succeeded"},
    {"cmd": CMD_SEND_NACK, "direction": "server_to_client", "purpose": "send_failed"},
    {"cmd": CMD_EDIT_MSG, "direction": "client_to_server", "purpose": "edit_message"},
    {"cmd": CMD_DELETE_MSG, "direction": "client_to_server", "purpose": "delete_message"},
    {
        "cmd": CMD_SESSION_ACTIVITY_SET,
        "direction": "client_to_server",
        "purpose": "session_activity_update",
    },
    {"cmd": CMD_LOCAL_ACTION, "direction": "server_to_client", "purpose": "local_action_request"},
    {
        "cmd": CMD_LOCAL_ACTION_RESULT,
        "direction": "client_to_server",
        "purpose": "local_action_result",
    },
    {
        "cmd": CMD_SESSION_ROUTE_BIND,
        "direction": "client_to_server",
        "purpose": "bind_session_route",
    },
    {
        "cmd": CMD_SESSION_ROUTE_RESOLVE,
        "direction": "client_to_server",
        "purpose": "resolve_session_route",
    },
    {"cmd": CMD_AGENT_INVOKE, "direction": "client_to_server", "purpose": "invoke_backend_action"},
    {"cmd": CMD_AGENT_INVOKE_RESULT, "direction": "server_to_client", "purpose": "invoke_backend_action_result"},
    {"cmd": CMD_ERROR, "direction": "bidirectional", "purpose": "generic_error"},
)

# Capabilities
CAP_SESSION_ROUTE = "session_route"
CAP_THREAD_V1 = "thread_v1"
CAP_INBOUND_MEDIA_V1 = "inbound_media_v1"
CAP_LOCAL_ACTION_V1 = "local_action_v1"
CAP_AGENT_INVOKE_V1 = "agent_invoke_v1"

REQUIRED_AUTH_CAPABILITIES = (CAP_LOCAL_ACTION_V1,)
STABLE_AUTH_CAPABILITIES = (
    CAP_SESSION_ROUTE,
    CAP_THREAD_V1,
    CAP_INBOUND_MEDIA_V1,
    CAP_LOCAL_ACTION_V1,
    CAP_AGENT_INVOKE_V1,
)

# Local actions
LOCAL_ACTION_EXEC_APPROVE = "exec_approve"
LOCAL_ACTION_EXEC_REJECT = "exec_reject"
LOCAL_ACTION_FILE_LIST = "file_list"
LOCAL_ACTION_CREATE_FOLDER = "create_folder"
STABLE_LOCAL_ACTIONS = (
    LOCAL_ACTION_EXEC_APPROVE,
    LOCAL_ACTION_EXEC_REJECT,
    LOCAL_ACTION_FILE_LIST,
    LOCAL_ACTION_CREATE_FOLDER,
)

# Status values
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_RESPONDED = "responded"
STATUS_STOPPED = "stopped"
STATUS_ALREADY_FINISHED = "already_finished"

STABLE_EVENT_RESULT_STATUSES = (
    STATUS_RESPONDED,
    STATUS_FAILED,
)
STABLE_EVENT_STOP_RESULT_STATUSES = (
    STATUS_STOPPED,
    STATUS_ALREADY_FINISHED,
    STATUS_FAILED,
)
STABLE_LOCAL_ACTION_RESULT_STATUSES = (
    STATUS_OK,
    STATUS_FAILED,
    STATUS_UNSUPPORTED,
)

# Error codes
ERR_INVALID_LOCAL_ACTION = "invalid_local_action"
ERR_UNSUPPORTED_LOCAL_ACTION = "unsupported_local_action"
ERR_MISSING_APPROVAL_ID = "missing_approval_id"
ERR_UNSUPPORTED_DECISION = "unsupported_decision"
ERR_APPROVAL_NOT_FOUND = "approval_not_found"
ERR_STOP_HANDLER_FAILED = "stop_handler_failed"

STABLE_ERROR_CODES = (
    ERR_INVALID_LOCAL_ACTION,
    ERR_UNSUPPORTED_LOCAL_ACTION,
    ERR_MISSING_APPROVAL_ID,
    ERR_UNSUPPORTED_DECISION,
    ERR_APPROVAL_NOT_FOUND,
    ERR_STOP_HANDLER_FAILED,
)

STABLE_PACKET_FIELDS = ("cmd", "seq", "payload")
REQUIRED_AUTH_FIELDS = ("agent_id", "api_key", "protocol_version", "contract_version")
FORBIDDEN_PUBLIC_FIELDS = (
    "chatid",
    "req_id",
    "markdown",
    "stream",
    "media_id",
    "upload_id",
)
RECOMMENDED_PUBLIC_FIELDS = (
    "agent_id",
    "session_id",
    "event_id",
    "msg_id",
    "thread_id",
    "route_session_key",
    "content",
    "quoted_message_id",
    "attachments",
    "mention_user_ids",
    "biz_card",
    "channel_data",
    "status",
    "code",
    "msg",
    "error_code",
    "error_msg",
)
MINIMAL_PLUGIN_SURFACE = (
    CMD_AUTH,
    CMD_PING,
    CMD_PONG,
    CMD_EVENT_MSG,
    CMD_EVENT_ACK,
    CMD_EVENT_RESULT,
    CMD_EVENT_STOP,
    CMD_EVENT_STOP_ACK,
    CMD_EVENT_STOP_RESULT,
    CMD_SEND_MSG,
    CMD_SEND_ACK,
    CMD_SEND_NACK,
    CMD_EDIT_MSG,
    CMD_DELETE_MSG,
    CMD_LOCAL_ACTION,
    CMD_LOCAL_ACTION_RESULT,
    CMD_SESSION_ROUTE_BIND,
    CMD_SESSION_ROUTE_RESOLVE,
    CMD_AGENT_INVOKE,
    CMD_AGENT_INVOKE_RESULT,
)


def public_command_names() -> tuple[str, ...]:
    return tuple(entry["cmd"] for entry in STABLE_PUBLIC_COMMANDS)


def build_public_contract_manifest() -> Dict[str, Any]:
    return {
        "protocol_version": AIBOT_PROTOCOL_VERSION,
        "contract_version": AIBOT_DEFAULT_CONTRACT_VERSION,
        "packet_fields": list(STABLE_PACKET_FIELDS),
        "public_commands": [dict(entry) for entry in STABLE_PUBLIC_COMMANDS],
        "required_auth_fields": list(REQUIRED_AUTH_FIELDS),
        "capabilities": {
            "required": list(REQUIRED_AUTH_CAPABILITIES),
            "stable": list(STABLE_AUTH_CAPABILITIES),
        },
        "local_actions": list(STABLE_LOCAL_ACTIONS),
        "statuses": {
            "event_result": list(STABLE_EVENT_RESULT_STATUSES),
            "event_stop_result": list(STABLE_EVENT_STOP_RESULT_STATUSES),
            "local_action_result": list(STABLE_LOCAL_ACTION_RESULT_STATUSES),
        },
        "error_codes": list(STABLE_ERROR_CODES),
        "forbidden_public_fields": list(FORBIDDEN_PUBLIC_FIELDS),
        "recommended_public_fields": list(RECOMMENDED_PUBLIC_FIELDS),
        "minimal_plugin_surface": list(MINIMAL_PLUGIN_SURFACE),
    }
