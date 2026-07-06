"""Grix platform adapter for Hermes Agent — plugin version.

This adapter connects Hermes to the Grix/aibot platform via websocket,
registering as a plugin platform so it works without modifying hermes-agent core.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import os
import time
from collections import defaultdict
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome, SendResult
from gateway.session import build_session_key

from .card_links import build_agent_question_card
from .compat import build_card_action_user_text, build_exec_approval_message
from .exec_command import parse_exec_command, handle_skills_command
from .question_command import parse_grix_question_command
from .tool_progress_cards import (
    build_tool_execution_channel_data,
    detect_hook_status,
    detect_tool_progress,
)
from .progress_cards import build_queue_progress_card
from .agent_status_cards import (
    build_agent_status_channel_data,
    detect_agent_status,
)
from .contract import (
    CMD_CONTROL_SHARE_SET,
    CMD_EVENT_CANCEL,
    CMD_EVENT_EDIT,
    CMD_EVENT_MSG,
    CMD_EVENT_REVOKE,
    CMD_EVENT_STOP,
    CMD_LOCAL_ACTION,
    CMD_QUEUE_CLEAR,
    ERR_APPROVAL_NOT_FOUND,
    ERR_INVALID_LOCAL_ACTION,
    ERR_MISSING_APPROVAL_ID,
    ERR_STOP_HANDLER_FAILED,
    ERR_UNSUPPORTED_DECISION,
    ERR_UNSUPPORTED_LOCAL_ACTION,
    LOCAL_ACTION_CONNECTOR_UPGRADE_PUSH,
    LOCAL_ACTION_EXEC_APPROVE,
    LOCAL_ACTION_EXEC_REJECT,
    LOCAL_ACTION_FILE_LIST,
    LOCAL_ACTION_GET_SESSION_USAGE,
    STATUS_ALREADY_FINISHED,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_RESPONDED,
    STATUS_STOPPED,
    STATUS_UNSUPPORTED,
)
from .protocol import (
    GrixConnectionConfig,
    GrixEditEvent,
    GrixEventCancelEvent,
    GrixInboundMessage,
    GrixLocalAction,
    GrixQueueClearEvent,
    GrixRevokeEvent,
    GrixStopEvent,
    build_connection_config,
    normalize_edit_event,
    normalize_event_cancel,
    normalize_inbound_message,
    normalize_local_action,
    normalize_queue_clear,
    normalize_revoke_event,
    normalize_stop_event,
)
from .transport import (
    AIOHTTP_AVAILABLE,
    GrixAuthRejectedError,
    GrixConnectionClosedError,
    GrixDependencyError,
    GrixPacketError,
    GrixTransportClient,
    GrixTransportError,
)

_PLATFORM_VALUE = "grix"

logger = logging.getLogger(__name__)

# agent 共享：handler 在入口把「正在处理本 packet 的 client」绑定到这个 ContextVar，
# 下游所有 send 通过 self._active_client() 取（contextvars 跨 await 自动透传，
# Python asyncio.create_task 默认拷贝当前 context，所以子任务里仍然能取到）。
# 未设置时**不再回退主连接**：脱离 packet 上下文的 send 会把消息错路由给主人身份，
# 造成 sender 错乱。此时一律 log + 返回 None，让调用失败比错发更安全。
# 真正需要主连接发起的管理性主动调用（如启动时的 skills 上报）显式走 self._client。
_CURRENT_CLIENT_CTX: ContextVar[Optional[GrixTransportClient]] = ContextVar(
    "grix_hermes_current_client", default=None
)

# 当前处理任务的 session_key：on_processing_start 时绑定，随 asyncio 任务链路 /
# 工具线程 context 拷贝传播。grix_reply 用它精确定位本次任务的应答目标（同一群
# 多个 per-user session 并发时 chat_id 无法消歧）。
_CURRENT_REPLY_SESSION_KEY: ContextVar[Optional[str]] = ContextVar(
    "grix_hermes_current_reply_session_key", default=None
)

_ROUTE_SESSION_KEY_PREFIX = "agent:main:grix:"
_EVENT_DEDUP_WINDOW_SECONDS = 300
_EVENT_DEDUP_MAX_SIZE = 1000

# agent 共享：adapter 是单进程多 WS，主连接和每个被共享者各一条连接，但 adapter 实例
# 只有一个。下面这些 per-chat / per-event 状态字典如果不按 owner 隔离，跨 owner 会串
# 数据(同一外部用户 X 同时是 owner A 和 owner B 的联系人时，DM session/审批/processing
# 状态会互相覆盖)。把所有 per-owner 状态收口到 _OwnerState，按 _CURRENT_CLIENT_CTX
# 解析出的 owner_key 分桶。主连接 owner_key=""（_PRIMARY_OWNER_KEY），共享子连接
# owner_key=shared_owner_id 字符串。
_PRIMARY_OWNER_KEY = ""


@dataclass
class _OwnerState:
    """单一 owner（主人 or 某个被共享者）维度的所有运行时状态。

    所有以 (chat_id / sender_id / session_id / message_id / event_id / approval_id /
    session_key) 为 key 的字典都按 owner 分桶，避免跨 owner 串数据。event_id /
    session_id 在 aibot 后端是 snowflake 全局唯一，但 dedup / 缓存依然按 owner 隔离
    以保证撤销共享后状态可干净清除（不影响其他 owner）。
    """

    completed_event_ids: Set[str] = field(default_factory=set)
    seen_event_ids: Dict[str, float] = field(default_factory=dict)
    completed_event_results: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)
    completed_stop_results: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)
    reply_event_ids: Dict[Tuple[str, str], str] = field(default_factory=dict)
    latest_sources: Dict[str, Any] = field(default_factory=dict)
    message_sources: Dict[Tuple[str, str], Any] = field(default_factory=dict)
    message_session_keys: Dict[Tuple[str, str], str] = field(default_factory=dict)
    user_dm_session_ids: Dict[str, str] = field(default_factory=dict)
    user_dm_session_keys: Dict[str, str] = field(default_factory=dict)
    approval_state: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)
    processing_message_ids: Dict[str, str] = field(default_factory=dict)
    revoked_message_keys: Set[Tuple[str, str]] = field(default_factory=set)
    busy_ack_msg_ids: Dict[str, Tuple[str, str, Any]] = field(default_factory=dict)
    tool_progress_msg_ids: Set[str] = field(default_factory=set)
    session_connector_hints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 正在处理中的任务的最终应答目标（session_key → chat_id / 触发消息 / 事件 / 来源
    # client / 主循环 loop）。grix_reply 工具据此自动补引用并路由回事件来源连接；
    # on_processing_start 写入、on_processing_complete 清除。
    active_reply_targets: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def check_grix_requirements() -> bool:
    return AIOHTTP_AVAILABLE


def _approval_lookup_id(params: Dict[str, Any]) -> str:
    return str(params.get("approval_id") or "").strip()


def _approval_choice_from_action(action_type: str, params: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    normalized_action = str(action_type or "").strip()
    decision = str(params.get("decision") or "").strip()
    if normalized_action == LOCAL_ACTION_EXEC_REJECT:
        return "deny", "deny"
    if normalized_action != LOCAL_ACTION_EXEC_APPROVE:
        return None, None

    if decision == "allow-once":
        return "once", decision
    if decision == "allow-always":
        return "always", decision
    if decision == "deny":
        return "deny", decision
    return None, decision or None


def build_grix_connection_config(config: PlatformConfig) -> GrixConnectionConfig:
    extra = dict(config.extra or {})
    api_key = config.api_key or config.token

    # Fallback to env vars when hermes config system hasn't populated extra
    if not extra.get("endpoint"):
        extra["endpoint"] = os.environ.get("GRIX_ENDPOINT", "").strip()
    if not extra.get("agent_id"):
        extra["agent_id"] = os.environ.get("GRIX_AGENT_ID", "").strip()
    if not api_key:
        api_key = os.environ.get("GRIX_API_KEY", "").strip()

    return build_connection_config(extra, api_key)


# 明确指向永久失败的 HTTP 状态码（客户端错误）。注意 408(超时) / 429(限流)
# 不在此列 —— 它们是瞬时的，仍应重试。
_NON_RETRYABLE_HTTP = frozenset({400, 401, 403, 404, 405, 406, 409, 410, 422})

# 文本层面明确指向永久失败的信号（鉴权/凭证类）。命中即放弃。
_NON_RETRYABLE_TOKENS = (
    "unauthorized",
    "forbidden",
    "invalid api key",
    "invalid token",
    "authentication failed",
    "auth rejected",
)


def _coerce_retryable(error: Exception) -> bool:
    """判断连接/发送错误是否值得重连。

    采用「默认可重试」的黑名单策略：只有明确指向永久失败的错误（鉴权拒绝、
    4xx 客户端错误）才放弃，其余一律重试。原因是代价不对称 —— 框架侧已有
    最多 20 次、退避封顶 5 分钟的上限，错判「可重试」最多多试几次就停；而
    错判「不可重试」会让平台被移出重试队列、彻底死连，必须人工重启容器。
    服务端 5xx / 408 / 429 是典型瞬时故障（网关滚动重启、过载），必须重试 ——
    旧白名单不含 HTTP 状态码，把 ws 握手 502/503 误判为永久失败，正是这次
    彻底掉线的直接原因。
    """
    # 鉴权拒绝：坏凭证不会因为重试而变好，永久失败。
    if isinstance(error, GrixAuthRejectedError):
        return False

    # 明确的瞬时类型：连接关闭、依赖缺失、握手限流(4008)。
    if isinstance(error, (GrixConnectionClosedError, GrixDependencyError)):
        return True
    if isinstance(error, GrixPacketError) and error.code == 4008:
        return True

    # HTTP 状态码：aiohttp 的 WSServerHandshakeError / ClientResponseError 带 .status。
    # 5xx 服务端错误、408 超时、429 限流 → 瞬时可重试；明确 4xx → 永久失败。
    status = getattr(error, "status", None)
    if isinstance(status, int):
        if status >= 500 or status in (408, 429):
            return True
        if status in _NON_RETRYABLE_HTTP:
            return False

    # 文本兜底：仅在命中明确的永久失败信号时才放弃。
    lowered = str(error).lower()
    if any(token in lowered for token in _NON_RETRYABLE_TOKENS):
        return False

    # 默认可重试（未知错误、空文本、各类网络/服务端瞬时故障都归此）。
    return True


def _resolve_message_type(message: GrixInboundMessage) -> MessageType:
    first = message.attachments[0] if message.attachments else None
    if not first:
        return MessageType.TEXT

    kind = (first.kind or "").lower()
    mime_type = (first.mime_type or "").lower()
    if kind == "image" or mime_type.startswith("image/"):
        return MessageType.PHOTO
    if kind == "video" or mime_type.startswith("video/"):
        return MessageType.VIDEO
    if kind == "voice" or mime_type in ("audio/ogg", "audio/opus", "audio/x-opus"):
        return MessageType.VOICE
    if kind == "audio" or mime_type.startswith("audio/"):
        return MessageType.AUDIO
    return MessageType.DOCUMENT


def _render_grix_context_block(message: GrixInboundMessage) -> str:
    """Assemble the backend-provided context_messages into readable text that is
    prepended to the agent prompt. A quoted (replied-to) entry has its content
    prefixed with "[引用消息]"; in group chats the sender id is included so the
    agent can tell who said what. 1:1 chats omit the sender id."""
    raw = message.raw or {}
    items = raw.get("context_messages")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except (ValueError, TypeError):
            return ""
    if not isinstance(items, list) or not items:
        return ""

    is_group = str(message.session_type or "") == "2"
    current_id = str(message.message_id or "")
    quoted_prefix = "[引用消息]"
    lines: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        msg_id = str(item.get("msg_id") or "")
        if msg_id and msg_id == current_id:
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        sender_id = str(item.get("sender_id") or "")
        if content.startswith(quoted_prefix):
            quoted = content[len(quoted_prefix):].lstrip("\n").strip()
            if is_group and sender_id:
                lines.append(f"[引用消息] (来自 {sender_id})：{quoted}")
            else:
                lines.append(f"[引用消息]：{quoted}")
        elif is_group and sender_id:
            lines.append(f"[{sender_id}]：{content}")
        else:
            lines.append(content)
    return "\n".join(lines)


def _is_record_only_message(message: GrixInboundMessage) -> bool:
    return str(getattr(message, "mirror_mode", "") or "").strip().lower() == "record_only"


def _source_field(source: Any, field: str) -> Optional[str]:
    if source is None:
        return None
    if isinstance(source, dict):
        value = source.get(field)
    else:
        value = getattr(source, field, None)
    return str(value).strip() if value else None


def _clone_metadata_object(metadata: Optional[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if not isinstance(value, dict) or not value:
        return None
    return copy.deepcopy(value)


def _lookup_grix_session_origin(session_key: str) -> Optional[Dict[str, Optional[str]]]:
    try:
        from hermes_constants import get_hermes_home
    except ImportError:
        return None

    sessions_path = get_hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.exists():
        return None
    try:
        with open(sessions_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.debug("[grix] Failed loading sessions.json for route lookup: %s", exc)
        return None

    entry = data.get(session_key) or {}
    origin = entry.get("origin") or {}
    if str(origin.get("platform") or "").strip() != _PLATFORM_VALUE:
        return None
    chat_id = str(origin.get("chat_id") or "").strip()
    if not chat_id:
        return None
    thread_id = str(origin.get("thread_id") or "").strip() or None
    return {"chat_id": chat_id, "thread_id": thread_id}


def _parse_route_session_key(value: str) -> Optional[Dict[str, Optional[str]]]:
    parts = str(value or "").strip().split(":")
    if len(parts) < 5 or parts[:3] != ["agent", "main", "grix"]:
        return None

    chat_type = parts[3]
    session_id = parts[4].strip()
    if not session_id:
        return None

    if chat_type == "dm":
        thread_id = ":".join(part for part in parts[5:] if part).strip() or None
        return {"chat_type": chat_type, "session_id": session_id, "thread_id": thread_id}

    if chat_type != "group":
        return None

    thread_id = None
    if len(parts) >= 7:
        thread_id = parts[5].strip() or None
    return {"chat_type": chat_type, "session_id": session_id, "thread_id": thread_id}


async def resolve_grix_target(
    client: Optional[GrixTransportClient],
    connection: GrixConnectionConfig,
    target: str,
    *,
    thread_id: Optional[str] = None,
    source_hint: Optional[Any] = None,
) -> tuple[str, Optional[str]]:
    raw_target = str(target or "").strip()
    if not raw_target:
        return raw_target, thread_id

    resolved_thread_id = str(thread_id).strip() if thread_id else None
    if source_hint is not None and not resolved_thread_id:
        hinted_thread_id = _source_field(source_hint, "thread_id")
        if hinted_thread_id:
            resolved_thread_id = hinted_thread_id

    if raw_target.startswith(_ROUTE_SESSION_KEY_PREFIX):
        if source_hint is None:
            persisted_source_hint = _lookup_grix_session_origin(raw_target)
            if persisted_source_hint is not None:
                source_hint = persisted_source_hint
                if not resolved_thread_id:
                    resolved_thread_id = _source_field(persisted_source_hint, "thread_id")

        parsed = _parse_route_session_key(raw_target)
        if parsed and not resolved_thread_id:
            resolved_thread_id = parsed.get("thread_id") or None

        if client:
            try:
                resolved = await client.resolve_session_route(
                    channel=_PLATFORM_VALUE,
                    account_id=connection.account_id,
                    route_session_key=raw_target,
                )
                resolved_session_id = str(resolved.get("session_id") or "").strip()
                if resolved_session_id:
                    return resolved_session_id, resolved_thread_id
            except Exception as exc:
                logger.debug("[grix] session_route_resolve failed for %s: %s", raw_target, exc)

        hinted_chat_id = _source_field(source_hint, "chat_id")
        if hinted_chat_id:
            return hinted_chat_id, resolved_thread_id
        if parsed:
            return str(parsed["session_id"]), resolved_thread_id
        return raw_target, resolved_thread_id

    if ":" in raw_target and not resolved_thread_id:
        session_id, inline_thread_id = raw_target.split(":", 1)
        session_id = session_id.strip()
        inline_thread_id = inline_thread_id.strip()
        if session_id and inline_thread_id:
            return session_id, inline_thread_id

    return raw_target, resolved_thread_id


class GrixAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 1800
    _SEND_MIN_INTERVAL = 0.5
    # 编辑消息的瞬时失败重试参数（覆盖 ws 内部重连窗口，约 3×3s）。
    _EDIT_RETRY_ATTEMPTS = 4
    _EDIT_RETRY_DELAY_S = 3.0

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(_PLATFORM_VALUE))
        self.connection = build_grix_connection_config(config)
        self._client: Optional[GrixTransportClient] = None
        self._connector = None
        self._disconnect_requested = False
        # 上次上报的技能集指纹：运行期按会话活动重扫时，仅在清单变化时才推 agent_skills_update。
        self._last_skills_hash: str = ""
        self._token_lock_identity: Optional[str] = None
        # agent 共享：所有 per-chat / per-event 状态收口到 _OwnerState，按当前 packet
        # 的 _CURRENT_CLIENT_CTX 解析出的 owner_key 分桶，跨 owner 物理隔离。详见
        # _OwnerState dataclass 与 _active_state() / _state_for() helper。
        self._owner_states: Dict[str, _OwnerState] = defaultdict(_OwnerState)
        self._last_send_at: float = 0.0
        self._send_lock = asyncio.Lock()
        self._reconnect_lock = asyncio.Lock()

        # agent 共享：为每个被共享者维护一条独立 WS 连接（key=shared_owner_id, value=client）。
        # 主连接收到 CMD_CONTROL_SHARE_SET 后 diff 名单增删；共享子连接复用 self._handle_protocol_packet
        # 处理回调，所有 send 通过 _active_client() 路由到「事件来源 client」（contextvars 透传）。
        self._shared_clients: Dict[str, GrixTransportClient] = {}
        # 串行化共享子连接的增删，避免并发 control_share_set 造成重复建/漏删。
        self._share_sync_lock = asyncio.Lock()
        # 关停标志：disconnect 期间禁止再为共享名单建新子连接，避免泄漏。
        self._shutting_down = False
        # 自升级检查器
        self._upgrade_checker: Optional["UpgradeChecker"] = None

    @staticmethod
    def _owner_key_of(client: Optional[GrixTransportClient]) -> str:
        """从 client 解析出 owner_key：共享子连接=shared_owner_id，主连接=""。"""
        if client is None:
            return _PRIMARY_OWNER_KEY
        # 真 client 用 _config（私有），测试用 FakeClient 暴露 config（公开），都兼容。
        cfg = getattr(client, "_config", None) or getattr(client, "config", None)
        shared = getattr(cfg, "shared_owner_id", None)
        if shared:
            s = str(shared).strip()
            if s:
                return s
        return _PRIMARY_OWNER_KEY

    def _active_owner_key(self) -> str:
        """当前 packet 上下文的 owner_key（依据 _CURRENT_CLIENT_CTX）。
        不在 packet handler 上下文时回落到主连接 — 这条路径只剩管理性主动调用，
        与「跨 owner 串数据」无关。"""
        return self._owner_key_of(_CURRENT_CLIENT_CTX.get())

    def _state_for(self, owner_key: str) -> _OwnerState:
        return self._owner_states[owner_key]

    def _active_state(self) -> _OwnerState:
        return self._state_for(self._active_owner_key())

    def _drop_owner_state(self, owner_key: str) -> None:
        """从 owner_states 移除某 owner 的全部状态（撤销共享 / 共享子连接关闭时调用）。"""
        self._owner_states.pop(owner_key, None)

    def _active_client(self) -> Optional[GrixTransportClient]:
        """返回「当前应使用的 client」：处理 packet 时回事件来源 client（共享子连接或主连接）。
        所有 send_* / complete_* / acknowledge_* 调用都应走这里，以保证共享子连接收到的事件，
        回执也从同一条连接发出（不串到主连接 / 不串给主人）。

        contextvar 未设置时**不再回退主连接** — 那会把消息按主人身份错发出去。
        改为 log error + 返回 None，让调用方失败（外层 try/except 会兜住 NoneType 异常）。
        需要主连接主动发起的场景（如启动 skills 上报）请显式用 self._client。"""
        ctx = _CURRENT_CLIENT_CTX.get()
        if ctx is None:
            logger.error(
                "[%s] _active_client called without packet ContextVar — refusing to fallback "
                "to primary client to avoid sender mix-up; call site must run inside a "
                "packet handler scope or use self._client explicitly",
                self.name,
            )
            return None
        return ctx

    def _bind_packet_handler(self, client: GrixTransportClient) -> None:
        """把 packet handler 绑定到 client，回调时携带 client 引用（让 _handle_protocol_packet
        知道事件从哪条连接来）。"""
        client.on_packet = lambda packet: self._handle_protocol_packet(packet, source_client=client)

    def format_message(self, content: str) -> str:
        return content.strip()

    @staticmethod
    def _message_size(content: str) -> int:
        return len(content.encode("utf-8"))

    async def _enforce_send_rate(self) -> None:
        async with self._send_lock:
            now = time.monotonic()
            wait = self._SEND_MIN_INTERVAL - (now - self._last_send_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send_at = time.monotonic()

    async def _detect_dead_transport(self) -> None:
        client = self._client
        if not self.is_connected or not client:
            return
        status = getattr(client, "status", None)
        if not isinstance(status, dict):
            return
        if not status.get("connected"):
            logger.warning("[%s] Transport is dead but adapter still connected, triggering reconnect", self.name)
            self._set_fatal_error("grix_transport_dead", "transport disconnected without notification", retryable=True)
            await self._notify_fatal_error()

    async def _get_ready_client(
        self,
        *,
        operation: str,
        require_authed: bool = True,
    ) -> Optional[GrixTransportClient]:
        # agent 共享: 在 packet handler 上下文中（contextvar 已 set 为事件来源 client）
        # 取该 client（共享子连接 / 主连接），就绪就直接用。
        # contextvar 未设置时**不再回退主连接** — 那会把消息按主人身份错发出去。
        # 直接 log error + 返回 None,让调用方失败。
        # 是否共享子连接以 config.shared_owner_id 是否非空为准（按身份语义判定），
        # 不能用 `ctx_client is self._client` 做对象身份比较：主连接 reconnect 后
        # self._client 会换成新对象，旧的 ctx_client 引用会变成「既不是主、shared_owner_id 也为空」
        # 的孤儿，被错判为共享子连接而拒绝 reconnect，整段 conversation 的 send 全部失败。
        ctx_client = _CURRENT_CLIENT_CTX.get()
        if ctx_client is None:
            logger.error(
                "[%s] %s called without packet ContextVar — refusing to fallback to primary "
                "client (would risk routing to wrong sender)",
                self.name,
                operation,
            )
            return None

        status = getattr(ctx_client, "status", None)
        if not isinstance(status, dict):
            return ctx_client

        connected = bool(status.get("connected"))
        authed = bool(status.get("authed"))
        if connected and (authed or not require_authed):
            return ctx_client

        ctx_shared_id = getattr(getattr(ctx_client, "_config", None), "shared_owner_id", None)
        is_shared_child = bool(ctx_shared_id)
        if is_shared_child:
            # 真共享子连接不就绪:不触发主连接 reconnect 路径(那只属于主连接),直接报失败给上层。
            logger.warning(
                "[%s] GRIX shared transport unavailable during %s shared_owner=%s connected=%s authed=%s",
                self.name,
                operation,
                ctx_shared_id,
                connected,
                authed,
            )
            return None

        # 主连接(含 reconnect 前的旧主连接引用)不就绪 — 走旧的内部 reconnect 路径,保持单连接场景的健壮性。
        if not self._disconnect_requested:
            if await self._try_reconnect_transport(
                reason=f"{operation}: transport not ready",
            ):
                return self._client

        if self.is_connected and not self._disconnect_requested:
            reason = str(status.get("last_error") or f"{operation}: transport is not connected")
            logger.warning(
                "[%s] GRIX transport unavailable during %s (connected=%s authed=%s): %s",
                self.name,
                operation,
                connected,
                authed,
                reason,
            )
            self._set_fatal_error("grix_transport_dead", reason, retryable=True)
            await self._notify_fatal_error()
        return None

    def _schedule_session_route_bind(self, *, session_key: str, session_id: str) -> None:
        # 在 packet handler 内调用,跟随事件来源 client(共享子连接 / 主连接);
        # 直接走 self._client 会让被共享者会话的 route_bind 跑到主连接上,造成绑错。
        client = _CURRENT_CLIENT_CTX.get()
        if client is None:
            logger.error(
                "[%s] _schedule_session_route_bind called without packet ContextVar — "
                "skipping bind to avoid routing to wrong client",
                self.name,
            )
            return

        async def _bind_route() -> None:
            try:
                await client.bind_session_route(
                    channel=_PLATFORM_VALUE,
                    account_id=self.connection.account_id,
                    route_session_key=session_key,
                    session_id=session_id,
                )
            except Exception as exc:
                logger.debug("[%s] GRIX session_route_bind failed: %s", self.name, exc)

        task = asyncio.create_task(_bind_route())
        try:
            self._background_tasks.add(task)
        except TypeError:
            return
        if hasattr(task, "add_done_callback"):
            task.add_done_callback(self._background_tasks.discard)

    async def _try_reconnect_transport(
        self, reason: str = "", max_attempts: int = 2
    ) -> bool:
        """Try to rebuild the WebSocket transport within the same adapter instance.

        This keeps the adapter alive so in-flight agent sessions can continue
        sending responses through the same adapter reference, avoiding the
        "transport not connected" failure caused by gateway adapter replacement.
        """
        async with self._reconnect_lock:
            # Double-check: another coroutine may have already reconnected.
            client = self._client
            if client:
                s = getattr(client, "status", None)
                if isinstance(s, dict) and s.get("connected") and s.get("authed"):
                    return True

            logger.info(
                "[%s] Internal transport reconnect: %s", self.name, reason or "unknown"
            )

            # Tear down the old (disconnected) client.
            old = self._client
            self._client = None
            if old:
                with suppress(Exception):
                    await old.disconnect(reason or "internal reconnect")

            for attempt in range(1, max_attempts + 1):
                try:
                    new_client = GrixTransportClient(
                        self.connection,
                        connector=self._connector,
                        on_status=self._handle_transport_status,
                    )
                    self._bind_packet_handler(new_client)
                    await new_client.connect()
                    self._client = new_client
                    self._mark_connected()
                    await self._report_skills()
                    await self._replay_pending_completed_events()
                    logger.info(
                        "[%s] Internal reconnect OK (attempt %d)",
                        self.name,
                        attempt,
                    )
                    return True
                except Exception as exc:
                    logger.warning(
                        "[%s] Internal reconnect attempt %d failed: %s",
                        self.name,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(2 * attempt)

            logger.error(
                "[%s] Internal reconnect failed after %d attempts",
                self.name,
                max_attempts,
            )
            return False

    async def connect(self, **kwargs) -> bool:
        if not self.connection.endpoint or not self.connection.agent_id or not self.connection.api_key:
            logger.error("[%s] Missing GRIX_ENDPOINT, GRIX_AGENT_ID, or GRIX_API_KEY", self.name)
            self._set_fatal_error(
                "grix_config_missing",
                "Missing GRIX_ENDPOINT, GRIX_AGENT_ID, or GRIX_API_KEY",
                retryable=False,
            )
            return False

        try:
            from gateway.status import acquire_scoped_lock

            self._token_lock_identity = (
                f"{self.connection.endpoint}|{self.connection.agent_id}|{self.connection.api_key}"
            )
            acquired, existing = acquire_scoped_lock(
                "grix-agent-credentials",
                self._token_lock_identity,
                metadata={"platform": _PLATFORM_VALUE, "endpoint": self.connection.endpoint},
            )
            if not acquired:
                owner_pid = existing.get("pid") if isinstance(existing, dict) else None
                message = "Grix connection settings already in use"
                if owner_pid:
                    message += f" (PID {owner_pid})"
                message += ". Stop the other gateway first."
                self._set_fatal_error("grix_token_lock", message, retryable=False)
                logger.error("[%s] %s", self.name, message)
                return False
        except Exception as exc:
            logger.warning("[%s] Failed to acquire GRIX lock: %s", self.name, exc)

        self._disconnect_requested = False
        self._client = GrixTransportClient(
            self.connection,
            connector=self._connector,
            on_status=self._handle_transport_status,
        )
        self._bind_packet_handler(self._client)
        try:
            await self._client.connect()
        except GrixAuthRejectedError as exc:
            self._set_fatal_error("grix_auth_rejected", str(exc), retryable=False)
            await self._safe_release_lock()
            return False
        except Exception as exc:
            self._set_fatal_error("grix_connect_failed", str(exc), retryable=_coerce_retryable(exc))
            await self._safe_release_lock()
            return False

        self._mark_connected()
        logger.info("[%s] Connected to %s", self.name, self.connection.endpoint)
        await self._report_skills()
        await self._start_upgrade_checker()
        return True

    async def _report_skills(self, *, force: bool = True) -> None:
        """扫描本地 skills 并通过 agent_skills_update 上报给后端。

        force=True（连接/重连）：无条件上报，重连后后端 profile 需要重建。
        force=False（会话活动触发）：仅当清单相对上次上报发生变化时才推，避免刷屏——
        用户新增/改名技能后无需整插件重启即可刷新工具栏清单。
        """
        try:
            from .exec_command import scan_hermes_skills
            entries = scan_hermes_skills()
            skills = [
                {"name": s.name, "description": s.description, "source": s.source}
                for s in entries
            ]
            if not skills:
                return
            digest = json.dumps([f"{s['source']}:{s['name']}" for s in skills])
            if not force and digest == self._last_skills_hash:
                return
            self._last_skills_hash = digest
            if self._client:
                # 启动时主连接的管理性主动调用,不在 packet handler 上下文,显式走主连接。
                await self._client.send_skills_update(skills)
                logger.info("[%s] Reported %d skill(s)", self.name, len(skills))
        except Exception as exc:
            logger.debug("[%s] Skills report failed: %s", self.name, exc)

    async def disconnect(self) -> None:
        self._disconnect_requested = True
        if self._upgrade_checker:
            self._upgrade_checker.stop()
            self._upgrade_checker = None
        # agent 共享：置位 shutting_down,串行等在途 share-set 同步结束,避免关停后泄漏。
        self._shutting_down = True
        async with self._share_sync_lock:
            shared_clients = list(self._shared_clients.values())
            self._shared_clients.clear()
        for shared in shared_clients:
            try:
                await shared.disconnect("adapter disconnect")
            except Exception as exc:
                logger.debug("[%s] GRIX shared client disconnect failed: %s", self.name, exc)
        client = self._client
        self._client = None
        if client:
            try:
                await client.disconnect("adapter disconnect")
            except Exception as exc:
                logger.debug("[%s] GRIX disconnect failed: %s", self.name, exc)
        # adapter 关停：所有 owner（主连接 + 全部被共享者）的 state 一次性清光。
        # 之前按 self._active_state().xxx.clear() 仅清了「active owner」(disconnect
        # 时 ContextVar 为 None → 落到主 owner)，而且只清了 6/16 个字段；其余 owner
        # 的 state + 主 owner 漏清的 10 个字段会全部残留，构成内存泄漏 + 重连后状态污染。
        self._owner_states.clear()
        await self._safe_release_lock()
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        *,
        force_quote: bool = False,
    ) -> SendResult:
        # 引用收敛（对齐 connector 语义）：过程消息一律不带引用——服务端把
        # 「agent 引用另一 agent 的消息」视为隐式 @ 并触发对方接活，流式过程里每条
        # 带引用的消息都会重复误触发。最终应答由 grix_reply 工具走 force_quote=True
        # 显式补引用；reply_to 仍保留用于 busy-ack 跟踪等内部匹配。
        client = await self._get_ready_client(operation="send")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)

        # Read per-session connector hints injected by the backend (e.g. group chat).
        _hints = self._active_state().session_connector_hints.get(str(chat_id)) or {}
        _drop_thinking = _hints.get("thinking_events") == "drop"
        _drop_tools = _hints.get("tool_events") == "drop"

        # Detect structured content and inject channel_data for card display.
        # Order matters: a gateway status line is checked first and short-circuits,
        # so it is never routed to the tool_execution path.  (Today's tool-progress
        # regex doesn't match these strings anyway, but ordering keeps the two
        # classifiers independent of future regex changes.)
        tp = None  # set only on the tool-progress path; consumed after send below
        status_text = detect_agent_status(content)
        if status_text:
            if _drop_thinking:
                # Backend instructed us to suppress thinking/status events.
                return SendResult(success=True, retryable=False)
            progress_card = build_queue_progress_card(status_text)
            if progress_card is not None:
                # 排队消息渲染为进度卡片：content 即 grix://card/progress
                # 链接，后端原样透传、前端渲染，无需 channel_data。
                content = progress_card
            else:
                if metadata is None:
                    metadata = {}
                else:
                    metadata = dict(metadata)
                cd: Dict[str, Any] = dict(metadata.get("channel_data") or {})
                cd.update(build_agent_status_channel_data(status_text))
                metadata["channel_data"] = cd
        else:
            tp = detect_tool_progress(content)
            if tp:
                if _drop_tools:
                    # Backend instructed us to suppress tool execution events.
                    return SendResult(success=True, retryable=False)
                tool_name, preview = tp
                if metadata is None:
                    metadata = {}
                else:
                    metadata = dict(metadata)
                cd = dict(metadata.get("channel_data") or {})
                cd.update(build_tool_execution_channel_data(tool_name, preview))
                metadata["channel_data"] = cd
            else:
                hs = detect_hook_status(content)
                if hs:
                    if _drop_tools:
                        return SendResult(success=True, retryable=False)
                    action_name, description = hs
                    if metadata is None:
                        metadata = {}
                    else:
                        metadata = dict(metadata)
                    cd = dict(metadata.get("channel_data") or {})
                    cd.update(build_tool_execution_channel_data(action_name, description))
                    metadata["channel_data"] = cd

        await self._enforce_send_rate()

        source_hint = self._active_state().latest_sources.get(str(chat_id))
        session_id, thread_id = await resolve_grix_target(
            client,
            self.connection,
            str(chat_id),
            thread_id=self._metadata_thread_id(metadata),
            source_hint=source_hint,
        )
        # NOTE: event_id is deliberately NOT included in send_text here.
        # Previously, the first streaming chunk carried event_id and called
        # _complete_event_if_needed, which closed the backend pending event
        # prematurely.  Subsequent final-response sends then hit 4003
        # "event_id not owned by current agent".
        # Event lifecycle is now managed at the handler level
        # (_handle_message_packet) instead.
        biz_card = _clone_metadata_object(metadata, "biz_card")
        channel_data = _clone_metadata_object(metadata, "channel_data")

        try:
            chunks = self.truncate_message(
                self.format_message(content),
                self.MAX_MESSAGE_LENGTH,
                len_fn=self._message_size,
            )
            receipt = None
            for index, chunk in enumerate(chunks):
                is_first = index == 0
                receipt = await client.send_text(
                    str(session_id),
                    chunk,
                    reply_to_message_id=reply_to if (is_first and force_quote) else None,
                    thread_id=thread_id,
                    biz_card=biz_card if is_first else None,
                    channel_data=channel_data if is_first else None,
                )
                if len(chunks) > 1 and index < len(chunks) - 1:
                    await asyncio.sleep(0.2)
            result = SendResult(
                success=bool(receipt and receipt.get("ok")),
                message_id=receipt.get("message_id") if receipt else None,
                raw_response=receipt,
                retryable=False,
            )
            # Track tool progress messages so edit_message can intercept them.
            if tp and result.success and result.message_id:
                self._active_state().tool_progress_msg_ids.add(result.message_id)
            if result.message_id and reply_to:
                _normalized_reply_to = str(reply_to).strip()
                for _sk, _pe in self._pending_messages.items():
                    if _pe and str(getattr(_pe, "message_id", "")).strip() == _normalized_reply_to:
                        # 记下发送 busy-ack 的 client,后续删除时要从同一条连接发删除指令,
                        # 避免脱离 packet 上下文后走错连接(可能错走主连接 / 共享子连接)。
                        self._active_state().busy_ack_msg_ids[_sk] = (str(chat_id), result.message_id, client)
                        logger.debug(
                            "[%s] Tracked busy-ack notification msg_id=%s for session_key=%s",
                            self.name, result.message_id, _sk,
                        )
                        break
            return result
        except Exception as exc:
            return SendResult(
                success=False,
                error=str(exc),
                raw_response=exc,
                retryable=_coerce_retryable(exc),
            )

    async def send_final_reply(
        self,
        *,
        chat_id: str,
        content: str,
        quoted_message_id: Optional[str] = None,
        source_client: Optional[GrixTransportClient] = None,
    ) -> SendResult:
        """发送任务的最终应答：带引用（引用触发消息即完成信号，可触发下一个 agent
        接活），并显式还原事件来源连接的 ContextVar（工具 handler 脱离 packet 上下文，
        不能靠隐式路由）。供 grix_reply 工具调用。"""
        token = _CURRENT_CLIENT_CTX.set(source_client) if source_client is not None else None
        try:
            return await self.send(
                str(chat_id),
                content,
                reply_to=quoted_message_id,
                force_quote=bool(quoted_message_id),
            )
        finally:
            if token is not None:
                _CURRENT_CLIENT_CTX.reset(token)

    async def send_clarify(
        self,
        chat_id: str,
        question: str,
        choices: Optional[List[str]],
        clarify_id: str,
        session_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """覆写基类的纯文本编号列表：choices 非空时发标准 agent_question 提问卡
        （服务端识别为 waiting_question，客户端渲染成可点选项），而不是靠文本猜测。

        答案回流复用现成机制、不需要新的入站代码：点击卡片选项 → 平台把 card_action
        转成普通文本消息（已有的 build_card_action_user_text 路径）→ 落进
        mark_awaiting_text 打开的会话级文本拦截（tools.clarify_gateway），和基类
        的纯文本兜底走的是同一条已验证的回收路径。开放式提问（choices 为空）
        与基类行为一致，直接发无卡片文本。
        """
        if not choices:
            return await self.send(str(chat_id), f"❓ {question}", metadata=metadata)

        client = await self._get_ready_client(operation="send_clarify")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)

        from tools.clarify_gateway import mark_awaiting_text
        mark_awaiting_text(clarify_id)

        source_hint = self._active_state().latest_sources.get(str(chat_id))
        session_id, thread_id = await resolve_grix_target(
            client,
            self.connection,
            str(chat_id),
            thread_id=self._metadata_thread_id(metadata),
            source_hint=source_hint,
        )
        card_content = build_agent_question_card(clarify_id, question, options=choices)
        try:
            receipt = await client.send_text(
                str(session_id),
                card_content,
                thread_id=thread_id,
            )
            return SendResult(
                success=bool(receipt.get("ok")),
                message_id=receipt.get("message_id"),
                raw_response=receipt,
                retryable=False,
            )
        except Exception as exc:
            return SendResult(
                success=False,
                error=str(exc),
                raw_response=exc,
                retryable=_coerce_retryable(exc),
            )

    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
        approval_id: Optional[str] = None,
    ) -> SendResult:
        client = await self._get_ready_client(operation="send_exec_approval")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)

        resolved_approval_id = str(approval_id or "").strip()
        if not resolved_approval_id:
            resolved_approval_id = f"ga_{abs(hash((session_key, command))) & 0xFFFFFFFF:08x}"

        source_hint = self._active_state().latest_sources.get(str(chat_id))
        session_id, thread_id = await resolve_grix_target(
            client,
            self.connection,
            str(chat_id),
            thread_id=self._metadata_thread_id(metadata),
            source_hint=source_hint,
        )
        raw_approval_data = None
        if isinstance(metadata, dict):
            candidate = metadata.get("approval_data")
            if isinstance(candidate, dict):
                raw_approval_data = candidate

        message = build_exec_approval_message(
            approval_id=resolved_approval_id,
            command=command,
            description=description,
            raw_approval_data=raw_approval_data,
        )

        try:
            receipt = await client.send_text(
                str(session_id),
                message.content,
                thread_id=thread_id,
                biz_card=message.biz_card,
                channel_data=message.channel_data,
            )
            self._active_state().approval_state[resolved_approval_id] = {
                "session_key": str(session_key).strip(),
                "chat_id": str(chat_id).strip(),
                "thread_id": thread_id,
            }
            return SendResult(
                success=bool(receipt.get("ok")),
                message_id=receipt.get("message_id"),
                raw_response=receipt,
                retryable=False,
            )
        except Exception as exc:
            return SendResult(
                success=False,
                error=str(exc),
                raw_response=exc,
                retryable=_coerce_retryable(exc),
            )

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        # Tool progress edits should become separate card messages.
        # Only intercept edits to messages we previously identified as
        # tool progress (tracked via _tool_progress_msg_ids) to avoid
        # false positives on regular streaming AI text.
        if message_id in self._active_state().tool_progress_msg_ids:
            self._active_state().tool_progress_msg_ids.discard(message_id)
            return SendResult(success=False, error="tool_progress_card_fallback")

        # Apply the same status-to-card conversion as send() so that heartbeat
        # edits (⏳ Working — N min…) render as progress cards rather than plain
        # text after the first send already established a card bubble.
        status_text = detect_agent_status(content)
        if status_text:
            progress_card = build_queue_progress_card(status_text)
            if progress_card is not None:
                content = progress_card

        _ = finalize
        # 编辑失败自动重试：hermes 网关把一次编辑失败视为「本轮编辑永久不可用」，
        # 之后所有内容都退化成不带引用的碎片补发（断句根因）。ws 内部重连期间的
        # 瞬时失败在这里就地等待并重试，让上层完全无感；只有明确的永久性错误
        # （如消息不存在）才立即上抛。
        last_error = "GRIX transport is not connected"
        last_raw: Any = None
        for attempt in range(self._EDIT_RETRY_ATTEMPTS):
            if attempt:
                await asyncio.sleep(self._EDIT_RETRY_DELAY_S)
            client = await self._get_ready_client(operation="edit_message")
            if not client:
                continue
            try:
                source_hint = self._active_state().latest_sources.get(str(chat_id))
                session_id, _thread_id = await resolve_grix_target(
                    client,
                    self.connection,
                    str(chat_id),
                    source_hint=source_hint,
                )
                receipt = await client.edit_message(
                    str(session_id),
                    str(message_id),
                    self.format_message(content),
                )
                return SendResult(
                    success=bool(receipt.get("ok")),
                    message_id=receipt.get("message_id"),
                    raw_response=receipt,
                    retryable=False,
                )
            except Exception as exc:
                # 服务端 4xxx NACK（参数错/消息不存在/权限拒绝，4008 握手限流除外）
                # 重试不会变好，立即上抛让上层走碎片补发，别浪费重试窗口。
                _nack_code = exc.code if isinstance(exc, GrixPacketError) else None
                _permanent_nack = (
                    isinstance(_nack_code, int) and 4000 <= _nack_code < 5000 and _nack_code != 4008
                )
                if _permanent_nack or not _coerce_retryable(exc):
                    return SendResult(
                        success=False,
                        error=str(exc),
                        raw_response=exc,
                        retryable=False,
                    )
                last_error = str(exc)
                last_raw = exc
        return SendResult(success=False, error=last_error, raw_response=last_raw, retryable=True)

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> SendResult:
        """通过 agent_invoke 通道删除消息。

        Hermes profile 不允许直接发 delete_msg 命令，必须走后端
        agent_invoke 接口。message-unsend skill 也走这条路径。
        """
        client = await self._get_ready_client(operation="delete_message")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)
        try:
            source_hint = self._active_state().latest_sources.get(str(chat_id))
            session_id, _thread_id = await resolve_grix_target(
                client,
                self.connection,
                str(chat_id),
                source_hint=source_hint,
            )
            result = await client.agent_invoke(
                action="delete_msg",
                params={
                    "session_id": str(session_id),
                    "msg_id": str(message_id),
                },
            )
            return SendResult(
                success=True,
                message_id=str(message_id),
                raw_response=result,
                retryable=False,
            )
        except Exception as exc:
            return SendResult(
                success=False,
                error=str(exc),
                raw_response=exc,
                retryable=_coerce_retryable(exc),
            )

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Grix 前端原生渲染 Markdown 图片。宿主网关会把回复里的 ![alt](url)
        # 抽出来改走本方法；基类兜底是「caption\nurl」纯文本（图片被降级成
        # 裸链接），所以这里必须还原成 Markdown 图片原样投递。
        alt = " ".join((caption or "").split())
        alt = alt.replace("[", "(").replace("]", ")")
        return await self.send(
            chat_id=chat_id,
            content=f"![{alt}]({image_url})",
            reply_to=reply_to,
            metadata=metadata,
        )

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        client = await self._get_ready_client(operation="send_typing")
        if not client:
            return
        try:
            source_hint = self._active_state().latest_sources.get(str(chat_id))
            session_id, _thread_id = await resolve_grix_target(
                client,
                self.connection,
                str(chat_id),
                thread_id=self._metadata_thread_id(metadata),
                source_hint=source_hint,
            )
            await client.set_session_activity(
                session_id=str(session_id),
                kind="composing",
                active=True,
                ttl_ms=self._metadata_ttl_ms(metadata),
                ref_message_id=self._metadata_ref_message_id(metadata),
                ref_event_id=self._metadata_ref_event_id(metadata),
            )
        except Exception as exc:
            logger.debug("[%s] GRIX typing update failed: %s", self.name, exc)

    async def stop_typing(self, chat_id: str) -> None:
        client = await self._get_ready_client(operation="stop_typing")
        if not client:
            return
        try:
            source_hint = self._active_state().latest_sources.get(str(chat_id))
            session_id, _thread_id = await resolve_grix_target(
                client,
                self.connection,
                str(chat_id),
                source_hint=source_hint,
            )
            await client.set_session_activity(
                session_id=str(session_id),
                kind="composing",
                active=False,
            )
        except Exception as exc:
            logger.debug("[%s] GRIX stop_typing failed: %s", self.name, exc)

    async def agent_invoke(
        self,
        *,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        client = await self._get_ready_client(operation="agent_invoke")
        if not client:
            raise RuntimeError("GRIX transport is not connected")
        return await client.agent_invoke(action=action, params=params, timeout_ms=timeout_ms)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        source = self._active_state().latest_sources.get(str(chat_id))
        if source:
            return {
                "id": source.chat_id,
                "name": source.chat_name or source.chat_id,
                "type": source.chat_type,
            }

        base_chat_id, _, thread_id = str(chat_id).partition(":")
        source = self._active_state().latest_sources.get(chat_id) or self._active_state().latest_sources.get(base_chat_id)
        if source:
            return {
                "id": source.chat_id,
                "name": source.chat_name or source.chat_id,
                "type": source.chat_type,
                **({"thread_id": thread_id} if thread_id else {}),
            }

        return {"id": str(chat_id), "name": str(chat_id), "type": "dm"}

    def get_pending_message(self, session_key: str) -> Optional[MessageEvent]:
        event = self._pending_messages.pop(session_key, None)
        if event:
            ack_entry = self._active_state().busy_ack_msg_ids.pop(session_key, None)
            if ack_entry:
                chat_id, msg_id, sender_client = ack_entry
                logger.debug(
                    "[%s] Scheduling busy-ack deletion msg_id=%s for session_key=%s",
                    self.name, msg_id, session_key,
                )
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self._delete_busy_ack(chat_id, msg_id, session_key, sender_client)
                    )
                except RuntimeError:
                    logger.warning(
                        "[%s] No running loop for busy-ack deletion msg_id=%s",
                        self.name, msg_id,
                    )
        return event

    async def _delete_busy_ack(
        self,
        chat_id: str,
        msg_id: str,
        session_key: str,
        sender_client: GrixTransportClient,
    ) -> None:
        # 显式把 ContextVar 还原为当初发送 busy-ack 的 client,确保 delete_message 内部
        # _get_ready_client() 走的是同一条连接(不靠脱离 packet 后的隐式 fallback)。
        token = _CURRENT_CLIENT_CTX.set(sender_client)
        try:
            try:
                result = await self.delete_message(chat_id, msg_id)
                logger.debug(
                    "[%s] Deleted busy-ack notification %s for session %s (success=%s)",
                    self.name, msg_id, session_key, result.success,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Failed to delete busy-ack notification %s: %s",
                    self.name, msg_id, exc,
                )
        finally:
            _CURRENT_CLIENT_CTX.reset(token)

    async def on_processing_complete(self, event: MessageEvent, outcome: ProcessingOutcome) -> None:
        raw_message = event.raw_message if isinstance(event.raw_message, dict) else {}
        if raw_message.get("_grix_kind") != "message":
            return
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        message_id = str(event.message_id or "").strip()
        if message_id and self._active_state().processing_message_ids.get(session_key) == message_id:
            self._active_state().processing_message_ids.pop(session_key, None)
        _reply_target = self._active_state().active_reply_targets.get(session_key)
        if _reply_target and (not message_id or _reply_target.get("message_id") == message_id):
            self._active_state().active_reply_targets.pop(session_key, None)
        if message_id and self.is_message_revoked(session_key, message_id):
            self._active_state().revoked_message_keys.discard((session_key, message_id))
            logger.debug(
                "[%s] Skipping completion for revoked GRIX message %s/%s",
                self.name,
                event.source.chat_id,
                message_id,
            )
            return
        event_id = raw_message.get("event_id")
        if not isinstance(event_id, str) or not event_id.strip():
            return

        is_success = outcome == ProcessingOutcome.SUCCESS or outcome is True
        status = STATUS_RESPONDED if is_success else STATUS_FAILED
        message = None if is_success else "message processing failed"
        await self._complete_event_if_needed(event_id.strip(), status=status, message=message)

    async def on_processing_start(self, event: MessageEvent) -> None:
        raw_message = event.raw_message if isinstance(event.raw_message, dict) else {}
        if raw_message.get("_grix_kind") != "message" or not event.message_id:
            return
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        self._active_state().processing_message_ids[session_key] = str(event.message_id)
        # 记录最终应答目标：grix_reply 工具在任务收尾时据此把最终总结发到正确会话
        # 并自动引用触发消息（引用即完成信号，可触发下一个 agent 接活）。工具 handler
        # 运行在独立线程/事件循环，这里顺带记下事件来源 client 与主循环 loop，发送时
        # 显式还原 ContextVar 并调度回主循环，避免跨 owner 串连接、跨 loop 竞态。
        try:
            _loop = asyncio.get_running_loop()
        except RuntimeError:
            _loop = None
        self._active_state().active_reply_targets[session_key] = {
            "session_key": session_key,
            "chat_id": str(event.source.chat_id),
            "message_id": str(event.message_id),
            "event_id": str(raw_message.get("event_id") or "").strip(),
            "client": _CURRENT_CLIENT_CTX.get(),
            "loop": _loop,
            "started_at": time.monotonic(),
        }
        # 同一群多个 per-user session 并发时 chat_id 无法消歧；把 session_key 绑到
        # 本次处理任务的 context（asyncio 任务链路 + 工具线程都会拷贝传播），
        # grix_reply 优先按它精确匹配。
        _CURRENT_REPLY_SESSION_KEY.set(session_key)

        # 运行期技能刷新：与 grix-connector 的 eventStarted 统一入口对齐——处理消息时
        # 按当前技能重扫，清单变化才上报，用户新增/改名技能无需重启插件即可刷新工具栏。
        await self._report_skills(force=False)

    def is_message_revoked(self, session_key: str, message_id: str) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not session_key or not normalized_message_id:
            return False
        return (session_key, normalized_message_id) in self._active_state().revoked_message_keys

    async def _try_resolve_question_reply(
        self,
        message: GrixInboundMessage,
        session_key: str,
    ) -> Tuple[bool, GrixInboundMessage]:
        """拦截服务端由提问卡点击改写来的 ``/grix question`` 命令。

        服务端把 agent_question_reply 卡片 URI 改写成
        ``/grix question <request_id> <答案>`` 后才投递给 hermes 系 agent
        （aibot backend/internal/agentadapter/hermes/adapter.go 的
        NormalizeOutbound → grixactions.RewriteToLegacyCommand，hermes 严格
        公有协议 profile 的约定）。request_id 就是 send_clarify 注册的
        clarify_id，按它直接解锁阻塞中的 clarify 线程。

        返回 ``(handled, message)``：解锁成功时 handled=True（事件已完成，
        调用方直接返回）；没有待解锁项（已超时/旧卡片）或不是该命令时
        handled=False——前者把可读答案文本替换进 message 继续走普通消息
        流程，模型仍能看到用户的选择，而不是被网关当未知命令吞掉。
        """
        parsed = parse_grix_question_command(message.text or "")
        if parsed is None:
            return False, message

        request_id, answer_text = parsed
        resolved = False
        try:
            from tools.clarify_gateway import resolve_gateway_clarify
            resolved = bool(resolve_gateway_clarify(request_id, answer_text))
        except Exception:
            logger.exception(
                "[%s] GRIX question reply resolve failed request_id=%s event_id=%s",
                self.name, request_id, message.event_id,
            )
        logger.info(
            "[%s] GRIX question reply intercepted request_id=%s resolved=%s event_id=%s session_key=%s",
            self.name, request_id, resolved, message.event_id, session_key,
        )
        if resolved:
            if self._client:
                await self._complete_event_if_needed(
                    message.event_id, status=STATUS_RESPONDED,
                )
            return True, message
        return False, dataclasses.replace(message, text=answer_text)

    def _build_record_only_attachment_summary(self, message: GrixInboundMessage) -> str:
        attachments = list(message.attachments or [])
        if not attachments:
            return ""

        labels = []
        for attachment in attachments[:3]:
            label = (
                str(attachment.file_name or "").strip()
                or str(attachment.kind or "").strip()
                or str(attachment.mime_type or "").strip()
                or "attachment"
            )
            labels.append(label)

        summary = ", ".join(labels)
        remaining = len(attachments) - len(labels)
        if remaining > 0:
            summary += f" (+{remaining} more)"
        return summary

    def _build_record_only_transcript_content(
        self,
        message: GrixInboundMessage,
        source: Any,
    ) -> str:
        if message.content_type == "card_action":
            content = build_card_action_user_text(
                message.card_action_tag or "button",
                message.card_action_value,
            )
        else:
            content = str(message.text or "").strip()

        attachment_summary = self._build_record_only_attachment_summary(message)
        if attachment_summary:
            suffix = f"[Attachments: {attachment_summary}]"
            content = f"{content}\n\n{suffix}" if content else suffix

        if not content:
            if message.content_type == "interactive_invalid":
                content = "[Recorded without processing: invalid interactive payload]"
            else:
                content = "[Recorded without processing: empty message]"

        thread_is_shared = (
            getattr(source, "chat_type", "") != "dm"
            and bool(getattr(source, "thread_id", None))
            and not self.config.extra.get("thread_sessions_per_user", False)
        )
        sender_name = str(getattr(source, "user_name", "") or "").strip()
        if thread_is_shared and sender_name:
            content = f"[{sender_name}] {content}"
        return content

    async def _record_message_without_processing(
        self,
        *,
        message: GrixInboundMessage,
        source: Any,
    ) -> None:
        session_store = getattr(self, "_session_store", None)
        if session_store is None:
            logger.warning(
                "[%s] Dropping record_only GRIX event %s: session store unavailable",
                self.name,
                message.event_id,
            )
            return

        session_entry = session_store.get_or_create_session(source)
        transcript_entry: Dict[str, Any] = {
            "role": "user",
            "content": self._build_record_only_transcript_content(message, source),
            "timestamp": datetime.now().isoformat(),
            "event_id": message.event_id,
            "message_id": message.message_id,
            "mirror_mode": message.mirror_mode,
            "_grix_kind": "message",
        }
        if message.reply_to_message_id:
            transcript_entry["reply_to_message_id"] = message.reply_to_message_id
        if message.thread_id:
            transcript_entry["thread_id"] = message.thread_id
        if message.attachments:
            transcript_entry["attachments"] = [
                {
                    "url": attachment.url,
                    "mime_type": attachment.mime_type,
                    "kind": attachment.kind,
                    "file_name": attachment.file_name,
                }
                for attachment in message.attachments
            ]

        session_store.append_to_transcript(session_entry.session_id, transcript_entry)

    def _make_shared_status_handler(self, shared_owner_id: str) -> Callable:
        """Return an on_status callback bound to a specific shared_owner_id.

        When the shared client disconnects unexpectedly, this triggers
        automatic reconnection — mirroring _handle_transport_status for
        the primary client."""

        async def handler(status: Dict[str, Any]) -> None:
            if self._disconnect_requested or self._shutting_down:
                return
            if status.get("connected", True):
                return
            reason = str(status.get("last_error") or "shared client disconnected")
            await self._try_reconnect_shared_client(shared_owner_id, reason=reason)

        return handler

    async def _try_reconnect_shared_client(
        self, shared_owner_id: str, *, reason: str = "", max_attempts: int = 2
    ) -> bool:
        """Try to reconnect a disconnected shared client.

        On auth rejection (share revoked) we clean up and stop.
        On transient failure we retry with exponential backoff."""
        async with self._share_sync_lock:
            if self._shutting_down or self._disconnect_requested:
                return False

            old_client = self._shared_clients.get(shared_owner_id)
            if old_client is None:
                # Already removed by control_share_set (revoked) or prior reconnect.
                return False
            s = getattr(old_client, "status", None)
            if isinstance(s, dict) and s.get("connected") and s.get("authed"):
                return True

            logger.info(
                "[%s] Shared client reconnect shared_owner=%s: %s",
                self.name,
                shared_owner_id,
                reason or "unknown",
            )

            self._shared_clients.pop(shared_owner_id, None)
            with suppress(Exception):
                await old_client.disconnect(reason or "shared client reconnect")

            for attempt in range(1, max_attempts + 1):
                if self._shutting_down or self._disconnect_requested:
                    return False
                try:
                    shared_config = dataclasses.replace(
                        self.connection, shared_owner_id=shared_owner_id
                    )
                    new_client = GrixTransportClient(
                        shared_config,
                        connector=self._connector,
                        on_status=self._make_shared_status_handler(shared_owner_id),
                    )
                    self._bind_packet_handler(new_client)
                    await new_client.connect()
                    self._shared_clients[shared_owner_id] = new_client
                    logger.info(
                        "[%s] Shared client reconnect OK shared_owner=%s (attempt %d)",
                        self.name,
                        shared_owner_id,
                        attempt,
                    )
                    return True
                except GrixAuthRejectedError:
                    logger.info(
                        "[%s] Shared client auth rejected (share revoked) shared_owner=%s",
                        self.name,
                        shared_owner_id,
                    )
                    self._drop_owner_state(shared_owner_id)
                    return False
                except Exception as exc:
                    logger.warning(
                        "[%s] Shared client reconnect attempt %d failed shared_owner=%s: %s",
                        self.name,
                        shared_owner_id,
                        attempt,
                        exc,
                    )
                    await asyncio.sleep(2 * attempt)

            logger.error(
                "[%s] Shared client reconnect failed after %d attempts shared_owner=%s",
                self.name,
                max_attempts,
                shared_owner_id,
            )
            return False

    async def _handle_transport_status(self, status: Dict[str, Any]) -> None:
        if self._disconnect_requested:
            return
        if status.get("connected", True):
            return
        if not self.is_connected:
            return

        message = str(status.get("last_error") or "grix websocket disconnected")

        # Try internal transport reconnection first — keeps the same adapter
        # instance alive so in-flight agent sessions can still send responses.
        if await self._try_reconnect_transport(reason=message):
            return

        # Internal reconnection failed; delegate to gateway adapter replacement.
        self._set_fatal_error("grix_connection_lost", message, retryable=True)
        await self._notify_fatal_error()

    async def _handle_protocol_packet(
        self,
        packet: Dict[str, Any],
        source_client: Optional[GrixTransportClient] = None,
    ) -> None:
        """处理一个 packet。source_client 指明事件来源连接（主或共享子连接），
        通过 ContextVar 透传给下游所有 send_*，确保回执从同一连接发出，
        不会把共享子连接收到的事件回到主连接（造成共享越权/串扰）。"""
        cmd = packet.get("cmd")
        payload = packet.get("payload") or {}
        token = _CURRENT_CLIENT_CTX.set(source_client) if source_client is not None else None
        try:
            if cmd == CMD_EVENT_MSG:
                await self._handle_message_packet(payload)
            elif cmd == CMD_LOCAL_ACTION:
                await self._handle_local_action_packet(payload)
            elif cmd == CMD_EVENT_STOP:
                await self._handle_stop_packet(payload)
            elif cmd == CMD_EVENT_EDIT:
                await self._handle_edit_packet(payload)
            elif cmd == CMD_EVENT_REVOKE:
                await self._handle_revoke_packet(payload)
            elif cmd == CMD_EVENT_CANCEL:
                await self._handle_event_cancel_packet(payload)
            elif cmd == CMD_QUEUE_CLEAR:
                await self._handle_queue_clear_packet(payload)
            elif cmd == CMD_CONTROL_SHARE_SET:
                # 共享名单仅主连接处理：共享子连接虽然也可能收到，但 diff 须由主实例统一做。
                if source_client is None or source_client is self._client:
                    await self._handle_share_set_packet(payload)
                else:
                    logger.debug(
                        "[%s] Ignoring %s on shared client (only primary handles diff)",
                        self.name,
                        cmd,
                    )
            else:
                logger.debug("[%s] Ignoring unknown GRIX packet %s", self.name, cmd)
        except Exception as exc:
            logger.error("[%s] Failed handling GRIX packet %s: %s", self.name, cmd, exc, exc_info=True)
        finally:
            if token is not None:
                _CURRENT_CLIENT_CTX.reset(token)

    async def _handle_share_set_packet(self, payload: Dict[str, Any]) -> None:
        """agent 共享：后端下发当前被共享者全量名单，diff 后增删共享子连接。
        每个被共享者一条独立 WS（主人 api_key + shared_owner_id），handler 回调
        通过 contextvar 路由到各自 client，确保回执不串。"""
        raw_list = payload.get("shared_to") or []
        if not isinstance(raw_list, list):
            logger.warning("[%s] control_share_set ignored: shared_to not list", self.name)
            return
        desired: set[str] = set()
        for item in raw_list:
            s = str(item).strip()
            if s:
                desired.add(s)

        async with self._share_sync_lock:
            current = set(self._shared_clients.keys())
            to_add = desired - current
            to_remove = current - desired

            # 新增：为名单中尚未运行的被共享者建独立 client。
            for shared_owner_id in to_add:
                if self._shutting_down:
                    break
                try:
                    shared_config = dataclasses.replace(
                        self.connection, shared_owner_id=shared_owner_id
                    )
                    shared_client = GrixTransportClient(
                        shared_config,
                        connector=self._connector,
                        on_status=self._make_shared_status_handler(shared_owner_id),
                    )
                    self._bind_packet_handler(shared_client)
                    await shared_client.connect()
                    self._shared_clients[shared_owner_id] = shared_client
                    logger.info(
                        "[%s] shared client connected agent=%s shared_owner=%s",
                        self.name,
                        self.connection.agent_id,
                        shared_owner_id,
                    )
                except Exception as exc:
                    logger.error(
                        "[%s] connect shared client failed shared_owner=%s: %s",
                        self.name,
                        shared_owner_id,
                        exc,
                    )

            # 移除：已不在名单中的子连接，断开并清理。
            for shared_owner_id in to_remove:
                shared_client = self._shared_clients.pop(shared_owner_id, None)
                if shared_client is None:
                    continue
                try:
                    await shared_client.disconnect("share revoked")
                except Exception as exc:
                    logger.warning(
                        "[%s] disconnect shared client failed shared_owner=%s: %s",
                        self.name,
                        shared_owner_id,
                        exc,
                    )
                # 共享被撤销后，对应 owner 的所有 per-chat/per-event 状态一并丢弃，
                # 防止后续若 owner 重新被授权时拿到旧残留（也避免长期累积内存）。
                self._drop_owner_state(shared_owner_id)
                logger.info(
                    "[%s] shared client disconnected agent=%s shared_owner=%s",
                    self.name,
                    self.connection.agent_id,
                    shared_owner_id,
                )

    async def _handle_local_action_packet(self, payload: Dict[str, Any]) -> None:
        if not self._active_client():
            return

        action: GrixLocalAction = normalize_local_action(payload)
        if not action.action_id or not action.action_type:
            await self._active_client().send_local_action_result(
                action_id=action.action_id or "unknown",
                status=STATUS_FAILED,
                error_code=ERR_INVALID_LOCAL_ACTION,
                error_message="missing action_id or action_type",
            )
            return

        if action.action_type == LOCAL_ACTION_FILE_LIST:
            await self._handle_file_list(action)
            return

        if action.action_type == LOCAL_ACTION_CONNECTOR_UPGRADE_PUSH:
            await self._handle_upgrade_push(action)
            return

        if action.action_type == LOCAL_ACTION_GET_SESSION_USAGE:
            await self._handle_get_session_usage(action)
            return

        if action.action_type not in {LOCAL_ACTION_EXEC_APPROVE, LOCAL_ACTION_EXEC_REJECT}:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_UNSUPPORTED,
                error_code=ERR_UNSUPPORTED_LOCAL_ACTION,
                error_message=f"unsupported local action: {action.action_type}",
            )
            return

        approval_id = _approval_lookup_id(action.params)
        if not approval_id:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_MISSING_APPROVAL_ID,
                error_message="approval_id is required",
            )
            return

        approval_choice, decision_value = _approval_choice_from_action(action.action_type, action.params)
        if approval_choice is None:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_UNSUPPORTED_DECISION,
                error_message=f"unsupported approval decision: {decision_value or action.action_type}",
            )
            return

        approval_state = self._active_state().approval_state.pop(approval_id, None)
        session_key = str((approval_state or {}).get("session_key") or "").strip()
        if not session_key:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_APPROVAL_NOT_FOUND,
                error_message="unknown or expired approval id",
            )
            return

        from tools.approval import resolve_gateway_approval

        resolved = resolve_gateway_approval(session_key, approval_choice)
        if resolved <= 0:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_APPROVAL_NOT_FOUND,
                error_message="unknown or expired approval id",
            )
            return

        paused_chat_id = str((approval_state or {}).get("chat_id") or "").strip()
        if paused_chat_id:
            self.resume_typing_for_chat(paused_chat_id)

        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=STATUS_OK,
            result=decision_value or approval_choice,
        )

    async def _handle_file_list(self, action: GrixLocalAction) -> None:
        from .file_list import handle_file_list_action, real_home_dir
        from .protocol import get_hostname

        if not self._active_client():
            return
        result = handle_file_list_action(
            action.params,
            fallback_dir=real_home_dir(),
        )
        # machine_name 在边界统一注入：仅当有 result（成功）时附加，与 grix-connector 对齐。
        payload = result.get("result")
        if payload is not None:
            payload = {**payload, "machine_name": get_hostname()}
        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=result["status"],
            result=payload,
            error_code=result.get("error_code"),
            error_message=result.get("error_msg"),
        )

    async def _handle_get_session_usage(self, action: GrixLocalAction) -> None:
        from .session_usage import handle_session_usage_action

        if not self._active_client():
            return
        hermes_home = self._resolve_hermes_home()
        result = handle_session_usage_action(action.params, hermes_home=hermes_home)
        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=result["status"],
            result=result.get("result"),
            error_code=result.get("error_code"),
            error_message=result.get("error_msg"),
        )

    def _resolve_hermes_home(self) -> str:
        try:
            from hermes_constants import get_hermes_home
            return str(get_hermes_home())
        except ImportError:
            return os.path.join(os.path.expanduser("~"), ".hermes")

    async def _start_upgrade_checker(self) -> None:
        try:
            from .upgrade_checker import UpgradeChecker

            self._upgrade_checker = UpgradeChecker(
                endpoint=self.connection.endpoint,
                api_key=self.connection.api_key,
                agent_id=self.connection.agent_id,
            )
            await self._upgrade_checker.start()
            logger.info("[%s] Upgrade checker started", self.name)
        except Exception as exc:
            logger.warning("[%s] Failed to start upgrade checker: %s", self.name, exc)

    async def _handle_upgrade_push(self, action: "GrixLocalAction") -> None:
        if not self._active_client():
            return
        if self._upgrade_checker:
            self._upgrade_checker.trigger_check()
        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=STATUS_OK,
            result="upgrade check triggered",
        )

    async def _handle_message_packet(self, payload: Dict[str, Any]) -> None:
        message = normalize_inbound_message(payload)
        source = self.build_source(
            chat_id=message.session_id,
            chat_name=message.chat_name,
            chat_type=message.chat_type,
            user_id=message.sender_id or None,
            user_name=message.sender_name or None,
            thread_id=message.thread_id,
            chat_topic=message.chat_topic,
        )
        session_key = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        prev_session_key = self._active_state().user_dm_session_keys.get(str(message.sender_id or "")) if message.chat_type == "dm" and message.sender_id else None
        prev_session_id = self._active_state().user_dm_session_ids.get(str(message.sender_id or "")) if message.chat_type == "dm" and message.sender_id else None
        self._active_state().latest_sources[message.session_id] = source
        self._active_state().latest_sources[session_key] = source
        if message.thread_id:
            self._active_state().latest_sources[f"{message.session_id}:{message.thread_id}"] = source
        self._active_state().reply_event_ids[(message.session_id, message.message_id)] = message.event_id
        self._active_state().message_sources[(message.session_id, message.message_id)] = source
        self._active_state().message_session_keys[(message.session_id, message.message_id)] = session_key

        # Extract extra.connector hints and index by both session_id and session_key
        # so send() can look them up by whichever key the caller uses as chat_id.
        raw_extra = payload.get("extra") or {}
        connector_hints = raw_extra.get("connector") or {} if isinstance(raw_extra, dict) else {}
        if connector_hints and isinstance(connector_hints, dict):
            self._active_state().session_connector_hints[message.session_id] = connector_hints
            self._active_state().session_connector_hints[session_key] = connector_hints

        if message.chat_type == "dm" and message.sender_id:
            sender_key = str(message.sender_id)
            self._active_state().user_dm_session_ids[sender_key] = str(message.session_id)
            self._active_state().user_dm_session_keys[sender_key] = session_key
            if prev_session_id and prev_session_id != str(message.session_id):
                logger.debug(
                    "[%s] GRIX DM session_id changed for user=%s old_session_id=%s new_session_id=%s old_session_key=%s new_session_key=%s event_id=%s message_id=%s",
                    self.name,
                    sender_key,
                    prev_session_id,
                    message.session_id,
                    prev_session_key,
                    session_key,
                    message.event_id,
                    message.message_id,
                )

        logger.debug(
            "[%s] GRIX inbound route event_id=%s message_id=%s sender_id=%s session_id=%s chat_type=%s thread_id=%s session_key=%s duplicate_check_pending=true",
            self.name,
            message.event_id,
            message.message_id,
            message.sender_id,
            message.session_id,
            message.chat_type,
            message.thread_id,
            session_key,
        )

        is_duplicate = self._remember_event_id(message.event_id)
        if is_duplicate:
            if self._client:
                await self._active_client().acknowledge_event(
                    event_id=message.event_id,
                    session_id=message.session_id,
                    message_id=message.message_id,
                )
                await self._replay_completed_event(message.event_id)
            logger.debug("[%s] Ignoring duplicate GRIX message event %s", self.name, message.event_id)
            return

        if self._client:
            await self._active_client().acknowledge_event(
                event_id=message.event_id,
                session_id=message.session_id,
                message_id=message.message_id,
            )
            self._schedule_session_route_bind(
                session_key=session_key,
                session_id=message.session_id,
            )

        # /stop 拦截：后端工具栏停止按钮通过 SendStopText 下发 "/stop" 文本命令
        if message.text and message.text.strip().lower() == "/stop":
            was_active = session_key in self._active_sessions
            logger.info(
                "[%s] GRIX /stop command received event_id=%s session_id=%s session_key=%s was_active=%s active_sessions=%s",
                self.name, message.event_id, message.session_id, session_key, was_active,
                list(self._active_sessions.keys()),
            )
            if was_active:
                await self._force_stop_session(
                    source, session_key, reply_to=message.message_id,
                )
            if self._client:
                await self._complete_event_if_needed(
                    message.event_id, status=STATUS_RESPONDED,
                )
            logger.info(
                "[%s] GRIX /stop command handled event_id=%s was_active=%s",
                self.name, message.event_id, was_active,
            )
            return

        # /grix exec interception — handle before normal message routing
        if message.text:
            parsed_exec = parse_exec_command(message.text)
            if parsed_exec is not None:
                subcommand, exec_args = parsed_exec
                if subcommand == "stop":
                    was_active = session_key in self._active_sessions
                    if was_active:
                        await self._force_stop_session(
                            source,
                            session_key,
                            reply_to=message.message_id,
                        )
                    result_text = "Session stopped." if was_active else "No active session to stop."
                elif subcommand == "skills":
                    result_text = handle_skills_command()
                else:
                    result_text = f"Unknown exec command: {subcommand}\nSupported: skills, stop"

                if self._client:
                    await self._active_client().send_text(
                        message.session_id,
                        result_text,
                        reply_to_message_id=message.message_id,
                        event_id=message.event_id,
                    )
                    await self._complete_event_if_needed(
                        message.event_id,
                        status=STATUS_RESPONDED,
                    )
                return

        # /grix question 拦截：服务端把提问卡的点击回复改写成旧式命令后才投递，
        # 不拦截的话 hermes 网关会把它当未知斜杠命令吞掉，clarify 只能等到超时。
        if message.text:
            handled, message = await self._try_resolve_question_reply(message, session_key)
            if handled:
                return

        if _is_record_only_message(message):
            await self._record_message_without_processing(
                message=message,
                source=source,
            )
            return

        if message.content_type == "interactive_invalid":
            logger.warning(
                "[%s] Malformed GRIX interactive payload for event %s, falling back to text",
                self.name,
                message.event_id,
            )

        event_text = message.text
        context_block = _render_grix_context_block(message)
        if context_block:
            event_text = f"{context_block}\n\n{event_text}" if event_text else context_block
        event_message_type = _resolve_message_type(message)
        raw_kind = "message"
        raw_message = {**message.raw}
        if message.content_type == "card_action":
            raw_kind = "card_action"
            raw_message["card_action"] = {
                "tag": message.card_action_tag or "button",
                "value": message.card_action_value,
            }

        event = MessageEvent(
            text=event_text,
            message_type=event_message_type,
            source=source,
            raw_message={**raw_message, "_grix_kind": raw_kind},
            message_id=message.message_id,
            media_urls=[attachment.url for attachment in message.attachments],
            media_types=[
                attachment.mime_type or attachment.kind or ""
                for attachment in message.attachments
            ],
            reply_to_message_id=message.reply_to_message_id,
        )

        try:
            if session_key not in self._active_sessions and message.message_id:
                self._active_state().processing_message_ids[session_key] = message.message_id
            await self.handle_message(event)
        except Exception as exc:
            if self._client:
                await self._complete_event_if_needed(
                    message.event_id,
                    status=STATUS_FAILED,
                    message=str(exc),
                )
            raise

        # Close the event after successful processing.
        # This must happen AFTER handle_message returns (all streaming +
        # final sends complete) so the backend pending event stays alive
        # throughout the entire response lifecycle.
        if self._client:
            await self._complete_event_if_needed(
                message.event_id, status=STATUS_RESPONDED,
            )

    async def _handle_edit_packet(self, payload: Dict[str, Any]) -> None:
        edit: GrixEditEvent = normalize_edit_event(payload)
        session_key = self._active_state().message_session_keys.get((edit.session_id, edit.message_id))
        if not session_key:
            source = self._active_state().latest_sources.get(edit.session_id)
            if source:
                session_key = build_session_key(
                    source,
                    group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                    thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
                )

        pending_event = self._pending_messages.get(session_key or "")
        if not pending_event or pending_event.message_id != edit.message_id:
            logger.debug(
                "[%s] GRIX edit for %s/%s has no pending Hermes event to update",
                self.name,
                edit.session_id,
                edit.message_id,
            )
            return

        pending_event.text = edit.text
        pending_event.reply_to_message_id = edit.reply_to_message_id
        pending_event.raw_message = {**edit.raw, "_grix_kind": "edit"}
        logger.debug(
            "[%s] Updated pending Hermes event from GRIX edit for %s/%s",
            self.name,
            edit.session_id,
            edit.message_id,
        )

    def _load_revoke_transcript(self, session_store: Any, session_id: str) -> list[Dict[str, Any]]:
        if hasattr(session_store, "get_transcript_path"):
            try:
                transcript_path = session_store.get_transcript_path(session_id)
                if transcript_path.exists():
                    messages: list[Dict[str, Any]] = []
                    with open(transcript_path, encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                messages.append(json.loads(line))
                            except json.JSONDecodeError:
                                logger.debug(
                                    "[%s] Skipping corrupt transcript line while handling revoke",
                                    self.name,
                                )
                    if messages:
                        return messages
            except Exception as exc:
                logger.debug("[%s] Could not load JSONL transcript for revoke: %s", self.name, exc)

        if hasattr(session_store, "load_transcript"):
            try:
                return list(session_store.load_transcript(session_id) or [])
            except Exception as exc:
                logger.debug("[%s] Could not load transcript for revoke: %s", self.name, exc)
        return []

    def _undo_last_completed_message_if_match(
        self,
        *,
        source: Any,
        session_key: Optional[str],
        message_id: str,
    ) -> bool:
        session_store = getattr(self, "_session_store", None)
        if session_store is None or source is None:
            return False
        if not hasattr(session_store, "get_or_create_session") or not hasattr(session_store, "rewrite_transcript"):
            return False

        try:
            session_entry = session_store.get_or_create_session(source)
        except Exception as exc:
            logger.debug("[%s] Could not resolve session for revoke undo: %s", self.name, exc)
            return False

        history = self._load_revoke_transcript(session_store, session_entry.session_id)
        last_user_idx = None
        for idx in range(len(history) - 1, -1, -1):
            if history[idx].get("role") == "user":
                last_user_idx = idx
                break
        if last_user_idx is None:
            return False

        last_user = history[last_user_idx]
        last_message_id = str(
            last_user.get("grix_message_id")
            or last_user.get("message_id")
            or ""
        ).strip()
        if last_message_id != str(message_id or "").strip():
            logger.debug(
                "[%s] GRIX revoke for %s did not match last user turn (%s); leaving history unchanged",
                self.name,
                message_id,
                last_message_id or "none",
            )
            return False

        session_store.rewrite_transcript(session_entry.session_id, history[:last_user_idx])
        if hasattr(session_store, "update_session") and session_key:
            try:
                session_store.update_session(session_key, last_prompt_tokens=0)
            except Exception:
                pass
        logger.info(
            "[%s] Rewound last completed GRIX turn for session %s message %s",
            self.name,
            getattr(session_entry, "session_id", "?"),
            message_id,
        )
        return True

    async def _handle_revoke_packet(self, payload: Dict[str, Any]) -> None:
        revoke: GrixRevokeEvent = normalize_revoke_event(payload)
        source = self._active_state().message_sources.get((revoke.session_id, revoke.message_id))
        session_key = self._active_state().message_session_keys.get((revoke.session_id, revoke.message_id))
        if not session_key and source is not None:
            session_key = build_session_key(
                source,
                group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )

        if self._client:
            await self._active_client().acknowledge_event(
                event_id=revoke.event_id,
                session_id=revoke.session_id,
                message_id=revoke.message_id,
            )

        pending_event = self._pending_messages.get(session_key or "")
        if pending_event and pending_event.message_id == revoke.message_id:
            self._pending_messages.pop(session_key, None)
            ack_entry = self._active_state().busy_ack_msg_ids.pop(session_key, None) if session_key else None
            if ack_entry:
                ack_chat_id, ack_msg_id, ack_sender_client = ack_entry
                await self._delete_busy_ack(
                    ack_chat_id or revoke.session_id,
                    ack_msg_id,
                    session_key or "",
                    ack_sender_client,
                )
            logger.debug(
                "[%s] Dropped pending Hermes event from GRIX revoke for %s/%s",
                self.name,
                revoke.session_id,
                revoke.message_id,
            )
        elif session_key and self._active_state().processing_message_ids.get(session_key) == revoke.message_id:
            self._active_state().revoked_message_keys.add((session_key, revoke.message_id))
            interrupt_event = self._active_sessions.get(session_key)
            if interrupt_event is not None:
                interrupt_event.set()
            try:
                await self.stop_typing(revoke.session_id)
            except Exception:
                pass
            logger.info(
                "[%s] Marked active GRIX message revoked for %s/%s",
                self.name,
                revoke.session_id,
                revoke.message_id,
            )
            if source is not None:
                self._undo_last_completed_message_if_match(
                    source=source,
                    session_key=session_key,
                    message_id=revoke.message_id,
                )
        elif source is not None:
            self._undo_last_completed_message_if_match(
                source=source,
                session_key=session_key,
                message_id=revoke.message_id,
            )

        self._active_state().reply_event_ids.pop((revoke.session_id, revoke.message_id), None)
        self._active_state().message_sources.pop((revoke.session_id, revoke.message_id), None)
        self._active_state().message_session_keys.pop((revoke.session_id, revoke.message_id), None)

    async def _handle_event_cancel_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 event_cancel：取消某个进行中的事件并上报结果。"""
        if not self._active_client():
            return

        try:
            cancel = normalize_event_cancel(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid event_cancel payload: %s", self.name, exc)
            return

        try:
            source = self._active_state().latest_sources.get(cancel.session_id)
            if source is None:
                source = self.build_source(chat_id=cancel.session_id, chat_type="dm")
            session_key = build_session_key(
                source,
                group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )
            await self._force_stop_session(
                source,
                session_key,
                reply_to=cancel.event_id,
            )
            await self._complete_event_if_needed(
                cancel.event_id,
                status=STATUS_FAILED,
                message="event canceled by user",
            )
            await self._active_client().send_event_cancel_result(
                event_id=cancel.event_id,
                accepted=True,
            )
        except Exception as exc:
            logger.error(
                "[%s] event_cancel handler failed for %s: %s",
                self.name,
                cancel.event_id,
                exc,
                exc_info=True,
            )
            try:
                await self._active_client().send_event_cancel_result(
                    event_id=cancel.event_id,
                    accepted=False,
                    reason=str(exc),
                )
            except Exception:
                pass

    async def _handle_queue_clear_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 queue_clear：清空某会话的待处理队列并上报结果。"""
        if not self._active_client():
            return

        try:
            clear = normalize_queue_clear(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid queue_clear payload: %s", self.name, exc)
            return

        try:
            # grix-hermes 是消息适配器，没有显式队列；按本地状态清空所有
            # 与该 session_id 关联的进行中处理与 pending 事件。
            # session_key 由 hermes-agent 拼成 "agent:main:<platform>:<chat_type>:<chat_id>[:<extra>]"
            # 形式，session_id 等于 chat_id，因此用冒号边界限定匹配，避免子串误伤。
            session_id = clear.session_id
            mid_marker = f":{session_id}:"
            end_marker = f":{session_id}"

            def _matches(key: str) -> bool:
                return mid_marker in key or key.endswith(end_marker)

            session_keys = [key for key in list(self._active_sessions) if _matches(key)]
            for key in session_keys:
                source = None
                pending = self._pending_messages.get(key)
                if pending is not None and getattr(pending, "source", None):
                    source = pending.source
                if source is None:
                    source = self._active_state().latest_sources.get(session_id)
                if source is None:
                    source = self.build_source(chat_id=session_id, chat_type="dm")
                await self._force_stop_session(source, key, reply_to=None)

            # 清掉残留 pending 事件（同样按 session_id 边界匹配）。
            # pending 事件已 ack 给平台，静默丢弃会让平台侧 durable run 与
            # 会话任务状态永远停留在 running（幽灵任务），必须逐条以终态收口。
            for key in list(self._pending_messages.keys()):
                if _matches(key):
                    dropped = self._pending_messages.pop(key, None)
                    event_id = getattr(dropped, "event_id", None) if dropped else None
                    if not event_id and dropped is not None:
                        event_id = self._active_state().reply_event_ids.get(
                            (session_id, str(getattr(dropped, "message_id", "") or ""))
                        )
                    if event_id:
                        await self._complete_event_if_needed(
                            str(event_id),
                            status=STATUS_FAILED,
                            message="canceled by queue clear",
                        )

            await self._active_client().send_queue_clear_result(
                session_id=session_id,
                success=True,
            )
        except Exception as exc:
            logger.error(
                "[%s] queue_clear handler failed for %s: %s",
                self.name,
                clear.session_id,
                exc,
                exc_info=True,
            )
            try:
                await self._active_client().send_queue_clear_result(
                    session_id=clear.session_id,
                    success=False,
                    message=str(exc),
                )
            except Exception:
                pass

    async def _handle_stop_packet(self, payload: Dict[str, Any]) -> None:
        stop = normalize_stop_event(payload)
        source = self._resolve_stop_source(stop)
        session_key = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        logger.info(
            "[%s] GRIX event_stop received event_id=%s stop_id=%s session_id=%s session_key=%s "
            "trigger_msg_id=%s stream_msg_id=%s reason=%s active_sessions=%s",
            self.name, stop.event_id, stop.stop_id, stop.session_id, session_key,
            stop.trigger_message_id, stop.stream_message_id, stop.reason,
            list(self._active_sessions.keys()),
        )
        was_active = await self._force_stop_session(
            source,
            session_key,
            reply_to=stop.trigger_message_id or stop.event_id,
        )
        logger.info(
            "[%s] GRIX event_stop force_stop done event_id=%s was_active=%s",
            self.name, stop.event_id, was_active,
        )

        is_duplicate = self._remember_event_id(stop.event_id)
        if is_duplicate:
            if self._client:
                await self._active_client().acknowledge_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    accepted=True,
                )
                await self._replay_completed_stop(stop.event_id, stop.stop_id)
            logger.info("[%s] GRIX event_stop duplicate, replayed event_id=%s", self.name, stop.event_id)
            return

        if self._client:
            await self._active_client().acknowledge_stop(
                event_id=stop.event_id,
                stop_id=stop.stop_id,
                accepted=True,
            )
            logger.info(
                "[%s] GRIX event_stop ack sent event_id=%s stop_id=%s",
                self.name, stop.event_id, stop.stop_id,
            )

        try:
            if self._client:
                final_status = STATUS_STOPPED if was_active else STATUS_ALREADY_FINISHED
                await self._complete_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    status=final_status,
                )
                logger.info(
                    "[%s] GRIX event_stop result sent event_id=%s stop_id=%s status=%s was_active=%s",
                    self.name, stop.event_id, stop.stop_id, final_status, was_active,
                )
        except Exception as exc:
            logger.error(
                "[%s] GRIX event_stop result failed event_id=%s stop_id=%s error=%s",
                self.name, stop.event_id, stop.stop_id, exc, exc_info=True,
            )
            if self._client:
                await self._complete_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    status=STATUS_FAILED,
                    code=ERR_STOP_HANDLER_FAILED,
                    message=str(exc),
                )
            raise

    async def _force_stop_session(
        self,
        source: Any,
        session_key: str,
        *,
        reply_to: Optional[str] = None,
    ) -> bool:
        was_active = session_key in self._active_sessions
        if not was_active:
            logger.debug(
                "[%s] _force_stop_session skip (not active) session_key=%s active_sessions=%s",
                self.name, session_key, list(self._active_sessions.keys()),
            )
            return False

        await self.cancel_session_processing(
            session_key,
            release_guard=True,
            discard_pending=True,
        )
        try:
            await self.stop_typing(source.chat_id)
        except Exception:
            pass

        thread_id = getattr(source, "thread_id", None)
        thread_meta = {"thread_id": thread_id} if thread_id else None
        try:
            await self._send_with_retry(
                chat_id=source.chat_id,
                content="⚡ Stopped. You can continue this session.",
                reply_to=reply_to,
                metadata=thread_meta,
            )
        except Exception as exc:
            logger.debug("[%s] Failed sending local stop confirmation for %s: %s", self.name, session_key, exc)

        logger.info("[%s] Locally stopped active GRIX session %s", self.name, session_key)
        return True

    def _resolve_stop_source(self, stop: GrixStopEvent):
        source = self._active_state().latest_sources.get(stop.session_id)
        if source:
            return source
        return self.build_source(
            chat_id=stop.session_id,
            chat_type=stop.chat_type,
        )

    async def _complete_event_if_needed(
        self,
        event_id: str,
        *,
        status: str,
        message: Optional[str] = None,
    ) -> None:
        if not self._client or not event_id or event_id in self._active_state().completed_event_ids:
            return
        try:
            await self._active_client().complete_event(
                event_id=event_id,
                status=status,
                message=message,
            )
        except Exception as exc:
            logger.debug(
                "[%s] GRIX complete_event failed for %s: %s",
                self.name,
                event_id,
                exc,
            )
            return
        self._active_state().completed_event_results[event_id] = {
            "status": status,
            "message": message,
        }
        self._active_state().completed_event_ids.add(event_id)

    async def _complete_stop(
        self,
        *,
        event_id: str,
        stop_id: Optional[str],
        status: str,
        code: Optional[str] = None,
        message: Optional[str] = None,
    ) -> None:
        if not self._client or not event_id:
            return
        await self._active_client().complete_stop(
            event_id=event_id,
            stop_id=stop_id,
            status=status,
            code=code,
            message=message,
        )
        self._active_state().completed_stop_results[event_id] = {
            "status": status,
            "stop_id": stop_id,
            "code": code,
            "message": message,
        }

    async def _replay_completed_event(self, event_id: str) -> None:
        if not self._active_client():
            return
        result = self._active_state().completed_event_results.get(event_id)
        if not result:
            return
        await self._active_client().complete_event(
            event_id=event_id,
            status=str(result.get("status") or STATUS_RESPONDED),
            message=result.get("message"),
        )

    async def _replay_pending_completed_events(self) -> None:
        """Re-send event_result for events that completed while WS was disconnected.

        When ``_complete_event_if_needed`` is called during a disconnect, the
        ``complete_event`` call silently fails but the event_id is still added
        to ``_completed_event_ids``.  On reconnect we re-emit those results so
        the backend can resolve the pending events via its durable storage.
        """
        if not self._client or not self._active_state().completed_event_ids:
            return
        replayed = 0
        for eid in list(self._active_state().completed_event_ids):
            result = self._active_state().completed_event_results.get(eid)
            if not result:
                continue
            try:
                await self._active_client().complete_event(
                    event_id=eid,
                    status=str(result.get("status") or STATUS_RESPONDED),
                    message=result.get("message"),
                )
                replayed += 1
            except Exception as exc:
                logger.debug(
                    "[%s] Re-play event_result for %s failed: %s",
                    self.name, eid, exc,
                )
        if replayed:
            logger.info(
                "[%s] Replayed %d completed event_result(s) after reconnect",
                self.name, replayed,
            )

    async def _replay_completed_stop(self, event_id: str, stop_id: Optional[str]) -> None:
        if not self._active_client():
            return
        result = self._active_state().completed_stop_results.get(event_id)
        if not result:
            return
        await self._active_client().complete_stop(
            event_id=event_id,
            stop_id=stop_id or result.get("stop_id"),
            status=str(result.get("status") or STATUS_ALREADY_FINISHED),
            code=result.get("code"),
            message=result.get("message"),
        )

    def _remember_event_id(self, event_id: str) -> bool:
        normalized_event_id = str(event_id or "").strip()
        if not normalized_event_id:
            return False

        now = time.time()
        if len(self._active_state().seen_event_ids) > _EVENT_DEDUP_MAX_SIZE:
            cutoff = now - _EVENT_DEDUP_WINDOW_SECONDS
            self._active_state().seen_event_ids = {
                key: ts for key, ts in self._active_state().seen_event_ids.items() if ts > cutoff
            }
            self._active_state().completed_event_results = {
                key: value
                for key, value in self._active_state().completed_event_results.items()
                if key in self._active_state().seen_event_ids
            }
            self._active_state().completed_stop_results = {
                key: value
                for key, value in self._active_state().completed_stop_results.items()
                if key in self._active_state().seen_event_ids
            }
            self._active_state().completed_event_ids = {
                key for key in self._active_state().completed_event_ids if key in self._active_state().seen_event_ids
            }

        if normalized_event_id in self._active_state().seen_event_ids:
            return True

        self._active_state().seen_event_ids[normalized_event_id] = now
        return False

    async def _safe_release_lock(self) -> None:
        if not self._token_lock_identity:
            return
        try:
            from gateway.status import release_scoped_lock

            release_scoped_lock("grix-agent-credentials", self._token_lock_identity)
        except Exception as exc:
            logger.debug("[%s] Failed releasing GRIX lock: %s", self.name, exc)
        finally:
            self._token_lock_identity = None

    @staticmethod
    def _metadata_thread_id(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
        if not metadata:
            return None
        value = metadata.get("thread_id")
        return str(value).strip() if value else None

    @staticmethod
    def _metadata_ttl_ms(metadata: Optional[Dict[str, Any]]) -> int:
        if not metadata:
            return 8_000
        try:
            value = int(metadata.get("ttl_ms", 8_000))
        except (TypeError, ValueError):
            value = 8_000
        return max(1_000, min(60_000, value))

    @staticmethod
    def _metadata_ref_message_id(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
        if not metadata:
            return None
        value = metadata.get("ref_msg_id")
        return str(value).strip() if value else None

    @staticmethod
    def _metadata_ref_event_id(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
        if not metadata:
            return None
        value = metadata.get("ref_event_id")
        return str(value).strip() if value else None
