"""Canonical definitions for the standard AIBOT protocol.

本模块是 grix-hermes 插件对外公开的 AIBOT 协议命令、能力、状态、错误码、
以及最小命令面的唯一来源。

字段以后端 Hermes 适配器的实际白名单为准（参见 backend
internal/ws/protocol/hermes_profile.go 与 internal/ws/agentapi/hermes_contract.go）。
"""

from __future__ import annotations

from typing import Any, Dict

AIBOT_PROTOCOL_VERSION = "aibot-agent-api-v1"
AIBOT_DEFAULT_CONTRACT_VERSION = 1

# 报文命令（与后端 hermes 通道完全对齐）
CMD_AUTH = "auth"
CMD_AUTH_ACK = "auth_ack"
CMD_PING = "ping"
CMD_PONG = "pong"
CMD_SEND_MSG = "send_msg"
CMD_SEND_ACK = "send_ack"
CMD_SEND_NACK = "send_nack"
CMD_ERROR = "error"
CMD_EDIT_MSG = "edit_msg"
CMD_UPDATE_BINDING_CARD = "update_binding_card"
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
# 事件生命周期命令（后端新增，APP 端用于事件取消与队列管理）
CMD_EVENT_CANCEL = "event_cancel"
CMD_EVENT_CANCEL_RESULT = "event_cancel_result"
CMD_QUEUE_CLEAR = "queue_clear"
CMD_QUEUE_CLEAR_RESULT = "queue_clear_result"
CMD_QUEUE_REORDER = "queue_reorder"
CMD_QUEUE_REORDER_RESULT = "queue_reorder_result"
CMD_EVENT_HOLD = "event_hold"
CMD_EVENT_HOLD_RESULT = "event_hold_result"
CMD_QUEUE_EDIT = "queue_edit"
CMD_QUEUE_EDIT_RESULT = "queue_edit_result"
CMD_EVENT_STATE = "event_state"
CMD_QUEUE_SNAPSHOT = "queue_snapshot"
CMD_QUEUE_SNAPSHOT_QUERY = "queue_snapshot_query"

# agent 共享：后端 → 主连接下行命令，载荷 {agent_id: str, shared_to: [str, ...]}。
# Hermes 收到后 diff 名单，为每个被共享者维护一条独立 WS 连接
# （主人 api_key + shared_owner_id）。仅主连接会收到。
CMD_CONTROL_SHARE_SET = "control_share_set"

# 服务端主动踢下线通知，载荷 {reason: str}。
CMD_KICKED = "kicked"

# 技能库变更提醒（beta 契约，docs/architecture/38 §6.2）：后端 → 主连接下行，
# 载荷 {owner_id: str, name?: str, version?: str}（name/version 仅供日志观测）。
# 收到即触发 SkillSyncer 立即下拉同步；轮询兜底仍在。
CMD_SKILL_SYNC = "skill_sync"

# auth_ack 错误码：agent 已删除或不存在（fatal，应永久停止重连；与 connector 语义一致）。
AUTH_CODE_AGENT_DELETED = 10008

# kicked 包 reason：agent 在线时被平台删除（与 auth_ack 10008 同等按 fatal 处理）。
KICKED_REASON_AGENT_DELETED = "agent_deleted"

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
    {"cmd": CMD_EVENT_CANCEL, "direction": "server_to_client", "purpose": "cancel_event_request"},
    {"cmd": CMD_EVENT_CANCEL_RESULT, "direction": "client_to_server", "purpose": "cancel_event_result"},
    {"cmd": CMD_QUEUE_CLEAR, "direction": "server_to_client", "purpose": "clear_event_queue_request"},
    {"cmd": CMD_QUEUE_CLEAR_RESULT, "direction": "client_to_server", "purpose": "clear_event_queue_result"},
    {"cmd": CMD_QUEUE_REORDER, "direction": "server_to_client", "purpose": "reorder_event_queue_request"},
    {"cmd": CMD_QUEUE_REORDER_RESULT, "direction": "client_to_server", "purpose": "reorder_event_queue_result"},
    {"cmd": CMD_EVENT_HOLD, "direction": "server_to_client", "purpose": "hold_or_release_queued_event_request"},
    {"cmd": CMD_EVENT_HOLD_RESULT, "direction": "client_to_server", "purpose": "hold_or_release_queued_event_result"},
    {"cmd": CMD_QUEUE_EDIT, "direction": "server_to_client", "purpose": "edit_queued_event_request"},
    {"cmd": CMD_QUEUE_EDIT_RESULT, "direction": "client_to_server", "purpose": "edit_queued_event_result"},
    {"cmd": CMD_EVENT_STATE, "direction": "client_to_server", "purpose": "report_event_state"},
    {"cmd": CMD_QUEUE_SNAPSHOT, "direction": "client_to_server", "purpose": "report_event_queue_snapshot"},
    {"cmd": CMD_QUEUE_SNAPSHOT_QUERY, "direction": "server_to_client", "purpose": "query_event_queue_snapshot"},
    {"cmd": CMD_EVENT_EDIT, "direction": "server_to_client", "purpose": "message_edit_event"},
    {"cmd": CMD_EVENT_REVOKE, "direction": "server_to_client", "purpose": "message_revoke_event"},
    {"cmd": CMD_SEND_MSG, "direction": "client_to_server", "purpose": "send_message"},
    {"cmd": CMD_SEND_ACK, "direction": "server_to_client", "purpose": "send_succeeded"},
    {"cmd": CMD_SEND_NACK, "direction": "server_to_client", "purpose": "send_failed"},
    {"cmd": CMD_EDIT_MSG, "direction": "client_to_server", "purpose": "edit_message"},
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
    {
        "cmd": CMD_UPDATE_BINDING_CARD,
        "direction": "client_to_server",
        "purpose": "update_toolbar_binding_metadata",
    },
    {"cmd": CMD_AGENT_INVOKE, "direction": "client_to_server", "purpose": "invoke_backend_action"},
    {"cmd": CMD_AGENT_INVOKE_RESULT, "direction": "server_to_client", "purpose": "invoke_backend_action_result"},
    {"cmd": CMD_ERROR, "direction": "bidirectional", "purpose": "generic_error"},
)

# 能力声明
CAP_SESSION_ROUTE = "session_route"
CAP_THREAD_V1 = "thread_v1"
CAP_INBOUND_MEDIA_V1 = "inbound_media_v1"
CAP_LOCAL_ACTION_V1 = "local_action_v1"
CAP_AGENT_INVOKE_V1 = "agent_invoke_v1"
CAP_STREAM_CHUNK = "stream_chunk"
# 终态投递持久化：与 grix-connector 同协议。后端须先支持 terminal_commit_v1 /
# stop-token，再升级本 Hermes 版本；勿另起一套协议。
CAP_EVENT_RESULT_ACK = "event_result_ack"
CAP_TERMINAL_COMMIT_V1 = "terminal_commit_v1"

REQUIRED_AUTH_CAPABILITIES = (CAP_LOCAL_ACTION_V1,)
STABLE_AUTH_CAPABILITIES = (
    CAP_STREAM_CHUNK,
    CAP_SESSION_ROUTE,
    CAP_THREAD_V1,
    CAP_INBOUND_MEDIA_V1,
    CAP_LOCAL_ACTION_V1,
    CAP_AGENT_INVOKE_V1,
    CAP_EVENT_RESULT_ACK,
    CAP_TERMINAL_COMMIT_V1,
)

# 本地动作（exec_approve/exec_reject/file_list 与后端 hermesSupportedLocalActions
# 对齐；get_session_usage / get_rate_limits 由插件声明，后端白名单同步纳入后生效）
LOCAL_ACTION_EXEC_APPROVE = "exec_approve"
LOCAL_ACTION_EXEC_REJECT = "exec_reject"
LOCAL_ACTION_FILE_LIST = "file_list"
LOCAL_ACTION_CREATE_FOLDER = "create_folder"
LOCAL_ACTION_GET_SESSION_USAGE = "get_session_usage"
LOCAL_ACTION_GET_RATE_LIMITS = "get_rate_limits"
LOCAL_ACTION_CONNECTOR_UPGRADE_PUSH = "connector_upgrade_push"
LOCAL_ACTION_SKILL_UPLOAD = "skill_upload"  # docs/architecture/39，工具栏一键上传技能
LOCAL_ACTION_SKILL_ENABLE = "skill_enable"  # 技能库启用到 Agent v2
LOCAL_ACTION_SKILL_DISABLE = "skill_disable"
LOCAL_ACTION_SKILL_REFRESH = "skill_refresh"  # 技能弹窗下拉刷新：重扫并重新上报 skills + library_skills
STABLE_LOCAL_ACTIONS = (
    LOCAL_ACTION_EXEC_APPROVE,
    LOCAL_ACTION_EXEC_REJECT,
    LOCAL_ACTION_FILE_LIST,
    LOCAL_ACTION_CREATE_FOLDER,
    LOCAL_ACTION_GET_SESSION_USAGE,
    LOCAL_ACTION_GET_RATE_LIMITS,
    LOCAL_ACTION_CONNECTOR_UPGRADE_PUSH,
    LOCAL_ACTION_SKILL_UPLOAD,
    LOCAL_ACTION_SKILL_ENABLE,
    LOCAL_ACTION_SKILL_DISABLE,
    LOCAL_ACTION_SKILL_REFRESH,
)

# 状态值
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_UNSUPPORTED = "unsupported"
STATUS_RESPONDED = "responded"
STATUS_CANCELED = "canceled"
STATUS_STOPPED = "stopped"
STATUS_ALREADY_FINISHED = "already_finished"

STABLE_EVENT_RESULT_STATUSES = (
    STATUS_RESPONDED,
    STATUS_FAILED,
    STATUS_CANCELED,
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

# 错误码
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
    CMD_EVENT_CANCEL,
    CMD_EVENT_CANCEL_RESULT,
    CMD_QUEUE_CLEAR,
    CMD_QUEUE_CLEAR_RESULT,
    CMD_EVENT_STATE,
    CMD_QUEUE_SNAPSHOT,
    CMD_SEND_MSG,
    CMD_SEND_ACK,
    CMD_SEND_NACK,
    CMD_EDIT_MSG,
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
