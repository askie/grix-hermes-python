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
import random
import re
import time
from collections import defaultdict
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
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
from .provider_quota import (
    ProviderQuotaSource,
    detect_provider,
    detect_provider_from_model,
    normalize_provider_id,
    provider_quota_to_rate_limits,
)
from .provider_quota_service import shared_provider_quota_service
from .agent_status_cards import (
    build_agent_status_channel_data,
    detect_agent_status,
)
from .contract import (
    AUTH_CODE_AGENT_DELETED,
    CMD_CONTROL_SHARE_SET,
    CMD_EVENT_CANCEL,
    CMD_EVENT_EDIT,
    CMD_EVENT_HOLD,
    CMD_EVENT_MSG,
    CMD_EVENT_REVOKE,
    CMD_EVENT_STOP,
    CMD_KICKED,
    CMD_LOCAL_ACTION,
    CMD_QUEUE_CLEAR,
    CMD_QUEUE_EDIT,
    CMD_QUEUE_REORDER,
    CMD_QUEUE_SNAPSHOT_QUERY,
    CMD_SKILL_SYNC,
    ERR_APPROVAL_NOT_FOUND,
    ERR_INVALID_LOCAL_ACTION,
    ERR_MISSING_APPROVAL_ID,
    ERR_STOP_HANDLER_FAILED,
    ERR_UNSUPPORTED_DECISION,
    ERR_UNSUPPORTED_LOCAL_ACTION,
    LOCAL_ACTION_CREATE_FOLDER,
    LOCAL_ACTION_CONNECTOR_UPGRADE_PUSH,
    LOCAL_ACTION_EXEC_APPROVE,
    LOCAL_ACTION_EXEC_REJECT,
    KICKED_REASON_AGENT_DELETED,
    LOCAL_ACTION_FILE_LIST,
    LOCAL_ACTION_GET_RATE_LIMITS,
    LOCAL_ACTION_GET_SESSION_USAGE,
    LOCAL_ACTION_SKILL_DISABLE,
    LOCAL_ACTION_SKILL_ENABLE,
    LOCAL_ACTION_SKILL_REFRESH,
    LOCAL_ACTION_SKILL_UPLOAD,
    STATUS_ALREADY_FINISHED,
    STATUS_CANCELED,
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
    normalize_event_hold,
    normalize_inbound_message,
    normalize_local_action,
    normalize_queue_clear,
    normalize_queue_edit,
    normalize_queue_reorder,
    normalize_queue_snapshot_query,
    normalize_revoke_event,
    normalize_stop_event,
    resolve_event_queue_settings,
)
from .terminal_paths import (
    build_terminal_outbox_path,
    resolve_terminal_sidecar_paths,
    suffix_shared_path,
)
from .event_queue import (
    EventQueue,
    EventQueueConfig,
    QueueItem,
    STATE_CANCELED as QUEUE_STATE_CANCELED,
    STATE_FAILED as QUEUE_STATE_FAILED,
    STATE_QUEUED as QUEUE_STATE_QUEUED,
    STATE_RUNNING as QUEUE_STATE_RUNNING,
    build_preview,
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

_RECONNECT_BASE_DELAY_SECONDS = 2.0
_RECONNECT_MAX_DELAY_SECONDS = 30.0
_RECONNECT_JITTER_RATIO = 0.2
_BACKGROUND_RECONNECT_BASE_DELAY_SECONDS = 30.0
_BACKGROUND_RECONNECT_MAX_DELAY_SECONDS = 300.0


def _reconnect_delay_seconds(
    attempt: int,
    *,
    base_delay_seconds: float = _RECONNECT_BASE_DELAY_SECONDS,
    max_delay_seconds: float = _RECONNECT_MAX_DELAY_SECONDS,
) -> float:
    """Return capped exponential backoff with jitter for reconnect attempts."""
    base_delay = min(max(0.0, base_delay_seconds), max_delay_seconds)
    # Saturating multiplication avoids constructing 2**attempt. A platform
    # may remain offline for days, so attempt can grow large enough for direct
    # exponentiation to overflow before min() gets a chance to cap it.
    for _ in range(max(0, attempt - 1)):
        if base_delay >= max_delay_seconds:
            break
        base_delay = min(base_delay * 2.0, max_delay_seconds)
    jitter = random.uniform(
        -_RECONNECT_JITTER_RATIO,
        _RECONNECT_JITTER_RATIO,
    )
    return min(
        max_delay_seconds,
        max(0.0, base_delay * (1.0 + jitter)),
    )


def _background_reconnect_delay_seconds(attempt: int) -> float:
    """Return long-running reconnect backoff with jitter."""
    return _reconnect_delay_seconds(
        attempt,
        base_delay_seconds=_BACKGROUND_RECONNECT_BASE_DELAY_SECONDS,
        max_delay_seconds=_BACKGROUND_RECONNECT_MAX_DELAY_SECONDS,
    )


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
    # 事件收口登记（对齐 connector：event_result 在任务真正结束时才发）。
    # open：session_key → 已投给 handle_message、尚未归属任何轮次的 event_id（到达顺序）。
    # next_run：session_key → 排队消息被消费（pending pop）时移入，归属下一轮任务。
    # running：session_key → 当前这轮后台任务认领的 event_id（含被合并进同一轮的
    # 排队事件）。归属链：open →(pending 消费)→ next_run →(on_processing_start)→
    # running →(on_processing_complete)→ 按任务真实结果收口。
    session_open_event_ids: Dict[str, List[str]] = field(default_factory=dict)
    session_next_run_event_ids: Dict[str, List[str]] = field(default_factory=dict)
    session_running_event_ids: Dict[str, List[str]] = field(default_factory=dict)
    # Hermes 框架已进入处理轮次、但显式 EventQueue 里未必还有 running 项的
    # 会话。连接器会把这类 self-driven activity 合成为一个虚拟 running 项，
    # 避免工具栏队列数在 agent 仍工作时错误归零；这里保存同等的运行态。
    # key=session_key，value={session_id, title, bg_hold?}。按 owner 分桶由 _OwnerState 保证。
    toolbar_active_work: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Hermes gateway 的 stream consumer 在部分代码路径（proxy 路径）中即使
    # SUPPORTS_MESSAGE_EDITING=False 也不会被跳过；它会先 send 一条 preview
    # 消息再反复 edit_message 更新。Grix 协议没有客户端编辑能力，preview 会
    # 变成「不完整消息气泡」留在对端。这里按 chat_id 缓冲 preview 帧，仅在
    # finalize 时一次性发出完整内容。
    # key=chat_id，value={content, reply_to, metadata, fake_message_id, updated_at}
    streaming_previews: Dict[str, Dict[str, Any]] = field(default_factory=dict)


class _PendingMessagesDict(dict):
    """带 pop 通知的 pending 队列容器。

    hermes 框架在多处直接 ``self._pending_messages.pop(...)`` 消费排队消息
    （轮末 drain、/new /reset 后 drain、runner 中途注入、停止丢弃），排队事件
    被合并后 event_id 会丢失，收口登记必须在「队列被消费」这一刻把归属定下来。
    需要绕过通知的内部清理请用 ``dict.pop(self._pending_messages, key, None)``。
    """

    def __init__(self, on_pop: Callable[[str, Any], None]):
        super().__init__()
        self._on_pop = on_pop

    def pop(self, key, *default):
        existed = key in self
        value = super().pop(key, *default)
        if existed:
            try:
                self._on_pop(key, value)
            except Exception:
                logger.debug("pending pop hook failed for %s", key, exc_info=True)
        return value


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

    base = build_connection_config(extra, api_key)
    explicit_outbox = str(extra.get("terminal_outbox_path") or "").strip() or None
    outbox_path = explicit_outbox or build_terminal_outbox_path(
        str(getattr(config, "name", None) or "hermes"),
        base.agent_id,
    )
    outbox_path, token_path, stop_path, committed_path = resolve_terminal_sidecar_paths(
        outbox_path,
        token_path=str(extra.get("terminal_commit_token_store_path") or "").strip() or None,
        stop_path=str(extra.get("stop_result_outbox_path") or "").strip() or None,
        committed_path=str(extra.get("terminal_committed_store_path") or "").strip() or None,
    )
    return dataclasses.replace(
        base,
        terminal_outbox_path=outbox_path,
        terminal_commit_token_store_path=token_path,
        stop_result_outbox_path=stop_path,
        terminal_committed_store_path=committed_path,
    )


def build_shared_connection_config(
    base: GrixConnectionConfig, shared_owner_id: str
) -> GrixConnectionConfig:
    """Derive a sharee connection with isolated terminal outbox paths."""
    owner = str(shared_owner_id or "").strip()
    return dataclasses.replace(
        base,
        shared_owner_id=owner,
        terminal_outbox_path=suffix_shared_path(base.terminal_outbox_path, owner),
        terminal_commit_token_store_path=suffix_shared_path(
            base.terminal_commit_token_store_path, owner
        ),
        stop_result_outbox_path=suffix_shared_path(base.stop_result_outbox_path, owner),
        terminal_committed_store_path=suffix_shared_path(
            base.terminal_committed_store_path, owner
        ),
    )


def resolve_configured_model(hermes_home: Optional[str] = None) -> str:
    """Read the immutable gateway model from the active Hermes profile."""
    home = Path(hermes_home or os.environ.get("HERMES_HOME", "") or "~/.hermes").expanduser()
    try:
        import yaml
    except ImportError:
        return ""
    try:
        with (home / "config.yaml").open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        return ""

    if not isinstance(config, dict):
        return ""
    raw_model = config.get("model")
    if isinstance(raw_model, str):
        return raw_model.strip()
    if isinstance(raw_model, dict):
        for key in ("default", "model", "name"):
            value = raw_model.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


_PLACEHOLDER_API_KEYS = frozenset(
    {
        "your-api-key",
        "your-api-key-1",
        "change_me",
        "changeme",
        "placeholder",
        "xxx",
        "sk-xxx",
    }
)


def _looks_like_placeholder_api_key(value: str) -> bool:
    """Inference relays often put a dummy model.api_key; quota needs the real key_env."""
    normalized = (value or "").strip()
    if not normalized:
        return True
    low = normalized.lower()
    if low in _PLACEHOLDER_API_KEYS:
        return True
    return low.startswith("your-")


def resolve_provider_quota_source(
    hermes_home: Optional[str] = None,
    environ: Optional[Dict[str, str]] = None,
) -> Optional[ProviderQuotaSource]:
    """解析当前生效 provider 的配额查询凭据。

    hermes 的 provider 凭据在 config.yaml（不在 grix agent config）：
    - 推理 base_url：model.base_url → providers[model.provider].api → GRIX_HERMES_BASE_URL
    - 推理 api_key：model.api_key → providers[model.provider].key_env → GRIX_PROVIDER_API_KEY

    配额查询与推理不同：本地中转（如 Antigravity 127.0.0.1）通常不暴露厂商
    配额 API，占位 api_key 也不能鉴权。因此：
    - model.api_key 若是占位符，回落到 key_env / GRIX_PROVIDER_API_KEY；
    - model.base_url 若无法识别厂商，而 providers[].api 可识别，则用后者查配额。
    凭据缺一即返回 None（不发起查询）。
    """
    env = os.environ if environ is None else environ
    home = Path(hermes_home or env.get("HERMES_HOME", "") or "~/.hermes").expanduser()
    try:
        import yaml
    except ImportError:
        return None
    try:
        with (home / "config.yaml").open("r", encoding="utf-8") as handle:
            config = yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError, ValueError, TypeError):
        return None
    if not isinstance(config, dict):
        return None

    raw_model = config.get("model")
    model = raw_model if isinstance(raw_model, dict) else {}
    model_name = ""
    if isinstance(raw_model, str):
        model_name = raw_model.strip()
    elif isinstance(raw_model, dict):
        for key in ("default", "model", "name"):
            value = raw_model.get(key)
            if isinstance(value, str) and value.strip():
                model_name = value.strip()
                break

    inference_base = str(model.get("base_url") or "").strip()
    model_api_key = str(model.get("api_key") or "").strip()
    provider_name = str(model.get("provider") or "").strip()

    providers = config.get("providers")
    entry = providers.get(provider_name) if isinstance(providers, dict) else None
    if not isinstance(entry, dict):
        entry = {}
    entry_api = str(entry.get("api") or "").strip()
    key_env = str(entry.get("key_env") or "").strip()
    env_key = env.get(key_env, "").strip() if key_env else ""

    if not inference_base:
        inference_base = entry_api
    if not inference_base:
        inference_base = env.get("GRIX_HERMES_BASE_URL", "").strip()

    api_key = "" if _looks_like_placeholder_api_key(model_api_key) else model_api_key
    key_from_env = False
    if not api_key:
        api_key = env_key
        key_from_env = bool(api_key)
    if not api_key:
        api_key = env.get("GRIX_PROVIDER_API_KEY", "").strip()
        key_from_env = bool(api_key)
    if not inference_base or not api_key:
        return None

    # Opaque inference relays → prefer vendor API URL only when the key also
    # came from key_env / GRIX_PROVIDER_API_KEY. A non-placeholder model.api_key
    # may be a local-relay token that must not be sent to the vendor domain.
    base_url = inference_base
    if (
        key_from_env
        and detect_provider(inference_base) is None
        and entry_api
        and detect_provider(entry_api)
    ):
        base_url = entry_api

    provider_id = (
        normalize_provider_id(provider_name)
        or (detect_provider(base_url) or (None,))[0]
        or (detect_provider(inference_base) or (None,))[0]
        or (detect_provider_from_model(model_name) or (None,))[0]
    )
    source: ProviderQuotaSource = {"baseUrl": base_url, "apiKey": api_key}
    if provider_id:
        source["providerId"] = provider_id
    return source


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


# 目录绑定指令（整条消息恰好是 grix://open/... URI）是平台生成的机器消息，
# 会被记入可见历史并随 context_messages 下发——注入 agent 上下文前过滤掉。
# 与 grix-connector 的 isOpenSessionDirectiveMessage 保持同一语义。
_OPEN_SESSION_DIRECTIVE_PATTERN = re.compile(r"^grix://open/\S+$", re.IGNORECASE)


def _is_open_session_directive(content: str) -> bool:
    return bool(_OPEN_SESSION_DIRECTIVE_PATTERN.match(content.replace("&amp;", "&").strip()))


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
        if _is_open_session_directive(content):
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


def _render_session_context_block(session_id: str) -> str:
    """One-shot [system-context] block carrying the Grix session_id, injected
    on the first message of a new (or auto-reset) hermes session. Text mirrors
    the connector's session-context block."""
    return "\n".join(
        [
            "[system-context]",
            f'Your current Grix session_id is "{session_id}".',
            "Use this id whenever you need to reference the current session.",
            "Treat this as an out-of-band instruction; do not echo or repeat it in your replies, and do not reply to acknowledge it.",
            "[/system-context]",
        ]
    )


def _is_new_hermes_session(entry: Any) -> bool:
    """Align with the core _is_new_session check (gateway/run.py): a session is
    fresh when created_at == updated_at, or when it was auto-reset (idle/daily)."""
    created = getattr(entry, "created_at", None)
    updated = getattr(entry, "updated_at", None)
    return (created is not None and created == updated) or bool(
        getattr(entry, "was_auto_reset", False)
    )


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
    # Grix/aibot 协议不支持编辑已发出的消息（没有客户端编辑能力）。
    # 声明此项可使 Hermes gateway 跳过逐 token 的流式编辑消费者，避免在
    # 对端产生「部分消息 + cursor + 完整消息」的重复气泡；最终回复走非流式
    # 路径统一发送一次，并自动带上对触发消息的引用。
    SUPPORTS_MESSAGE_EDITING = False
    MAX_MESSAGE_LENGTH = 1800
    _SEND_MIN_INTERVAL = 0.5
    # 编辑消息的瞬时失败重试参数（覆盖 ws 内部重连窗口，约 3×3s）。
    _EDIT_RETRY_ATTEMPTS = 4
    _EDIT_RETRY_DELAY_S = 3.0
    # LLM 轮次已 event_result 收口后，若 Hermes 后台进程仍在跑且仍会产生
    # 后续信号（完成通知/watch 命中），继续用虚拟 running 保活工具栏队列。
    # 超过此年龄或无后续信号的进程视为僵尸/常驻 daemon，不再挡队列收口。
    _BG_HOLD_MAX_AGE_S = 6 * 3600
    _BG_HOLD_SWEEP_INTERVAL_S = 15.0
    _BG_HOLD_COMPOSING_TTL_MS = 90_000
    # 对齐 connector PROVIDER_QUOTA_REFRESH_INTERVAL_MS = 30s
    _PROVIDER_QUOTA_REFRESH_INTERVAL_S = 30.0

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(_PLATFORM_VALUE))
        self.connection = build_grix_connection_config(config)
        # Hermes gateway sessions share one profile-level model. Freeze it at
        # adapter construction: unlike switchable CLI adapters, Hermes exposes
        # only this configured model in toolbar metadata.
        self._toolbar_model_id = resolve_configured_model()
        # 厂商用量限额缓存（对齐 connector provider-quota）：后台 30s 巡检刷新，
        # 随 _push_queue_snapshot 的工具栏绑定卡下发 provider_quota/rate_limits。
        self._provider_quota: Optional[Dict[str, Any]] = None
        self._provider_quota_sampled_at_ms: int = 0
        self._provider_quota_task: Optional[asyncio.Task] = None
        self._client: Optional[GrixTransportClient] = None
        self._connector = None
        self._disconnect_requested = False
        # agent 已在平台删除（auth_ack 10008 / kicked reason=agent_deleted）：置位后永久禁止重连。
        self._agent_deleted = False
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
        # Serializes the complete status-driven reconnect + fatal handoff.
        # _reconnect_lock alone only serializes transport rebuilds; without
        # this outer lock, already queued on_status callbacks can each perform
        # another rebuild after the first one gives up.
        self._status_reconnect_lock = asyncio.Lock()

        # agent 共享：为每个被共享者维护一条独立 WS 连接（key=shared_owner_id, value=client）。
        # 主连接收到 CMD_CONTROL_SHARE_SET 后 diff 名单增删；共享子连接复用 self._handle_protocol_packet
        # 处理回调，所有 send 通过 _active_client() 路由到「事件来源 client」（contextvars 透传）。
        self._shared_clients: Dict[str, GrixTransportClient] = {}
        self._desired_shared_owner_ids: Set[str] = set()
        self._shared_reconnect_tasks: Dict[str, asyncio.Task] = {}
        # 串行化共享子连接的增删，避免并发 control_share_set 造成重复建/漏删。
        self._share_sync_lock = asyncio.Lock()
        # 关停标志：disconnect 期间禁止再为共享名单建新子连接，避免泄漏。
        self._shutting_down = False
        # 自升级检查器
        self._upgrade_checker: Optional["UpgradeChecker"] = None
        self._skill_syncer: Optional["SkillSyncer"] = None
        # 正在 handle_message 派发途中的事件（session_key → event_id 集合）。
        # 收口扫尾/排队归属都跳过这些事件：它们可能马上会入队/被认领，
        # 提前定归属就回到"任务没结束先报完成"的老毛病。
        self._inflight_dispatch_event_ids: Dict[str, Set[str]] = {}
        # session_key -> hermes session_id that already got the one-shot
        # [system-context] block. In-memory only; after a restart existing
        # sessions are judged not-new (created_at != updated_at), so nothing
        # is re-injected. The hermes session_id rotates on auto-reset, which
        # is what triggers re-injection for a reset session.
        self._session_context_injected: Dict[str, str] = {}
        # 用带 pop 通知的容器替换框架的 pending 队列：排队消息被消费（drain /
        # 注入当前轮）那一刻，把它名下登记的事件移交给消费方（当前轮 running
        # 或下一轮 next_run），合并丢失 event_id 也不会漏收口。
        self._pending_messages = _PendingMessagesDict(self._on_pending_consumed)
        # 显式事件队列（对齐 connector 的 EventQueue）：每会话同时只执行一条
        # 消息事件，其余在这里排队，支持快照/单删/重排/清空。事件收口
        # （_complete_event_if_needed）释放槽位并续投下一条。纯内存态，进程
        # 重启即丢（与 connector 一致）。排队期间不额外点亮 composing——同会话
        # 必有一条运行中事件，其轮次的 typing 心跳已覆盖该会话的 composing。
        _queue_settings = self.connection.concurrency or resolve_event_queue_settings({})
        self._event_queue = EventQueue(
            EventQueueConfig(
                max_queued=int(_queue_settings["max_queued"]),
                queue_timeout_ms=int(_queue_settings["queue_timeout_ms"]),
                run_timeout_ms=int(_queue_settings["run_timeout_ms"]),
            ),
            on_deliver=self._on_queue_deliver,
            on_state_change=self._on_queue_state_change,
        )
        # complete() → drain → deliver 的同步汇聚桶。收口钩子里 await 续投，
        # 避免 create_task 把下一条投到「当前轮 late_pending 检查之后」，造成
        # pending 孤儿 + EventQueue running 槽空挂到 run_timeout（见 de89f921
        # 2026-07-23 事故：追问被 interrupt 进 pending，30 分钟后才失败收口）。
        self._sync_deliver_bucket: Optional[List[Any]] = None
        # 后台进程保活巡检：LLM 轮次结束后若 process_registry 仍有该会话进程，
        # 继续推虚拟 running；进程退出后清掉。
        self._bg_hold_sweep_task: Optional[asyncio.Task] = None

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

    def _clean_stale_streaming_previews(self, state: _OwnerState) -> None:
        """清理超时的流式 preview buffer，避免跨轮次/崩溃后残留导致误发。"""
        now = time.monotonic()
        _max_age = 300.0
        stale = [
            chat_id
            for chat_id, preview in state.streaming_previews.items()
            if now - preview.get("updated_at", 0) > _max_age
        ]
        for chat_id in stale:
            state.streaming_previews.pop(chat_id, None)

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

    def format_tool_event(
        self,
        event: Any,
        *,
        mode: str = "all",
        preview_max_len: int = 40,
    ) -> Optional[str]:
        """Render a ToolCallChunk as the raw ``emoji tool_name: "preview"`` line.

        Grix renders tool progress as a tool_execution card keyed on the tool
        name, so the adapter pins the machine-readable shape rather than the
        friendly prose the gateway shows elsewhere ("📖 Reading docs/api.md"),
        which carries no tool name.  ``detect_tool_progress`` still parses both
        shapes, because the gateway does not route tool events through this
        hook yet (``GatewayEventDispatcher`` is unwired upstream).
        """
        from gateway.stream_events import ToolCallChunk

        if not isinstance(event, ToolCallChunk):
            return None
        if mode == "verbose":
            return super().format_tool_event(
                event, mode=mode, preview_max_len=preview_max_len,
            )

        from agent.display import get_tool_emoji

        emoji = get_tool_emoji(event.tool_name, default="⚙️")
        preview = event.preview
        if not preview:
            return f"{emoji} {event.tool_name}..."
        cap = preview_max_len if preview_max_len > 0 else 40
        if len(preview) > cap:
            preview = preview[: cap - 3] + "..."
        return f'{emoji} {event.tool_name}: "{preview}"'

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

    def _new_primary_transport_client(self) -> GrixTransportClient:
        """Build a primary client whose status callback is source-aware.

        A failed authentication calls transport.disconnect(), which emits a
        disconnected status. Binding the source client lets the adapter ignore
        callbacks from failed/stale reconnect candidates instead of recursively
        scheduling more reconnects.
        """
        client = GrixTransportClient(
            self.connection,
            connector=self._connector,
            on_status=None,
        )

        async def _on_status(status: Dict[str, Any]) -> None:
            await self._handle_transport_status(status, source_client=client)

        client.on_status = _on_status
        return client

    async def _try_reconnect_transport(
        self, reason: str = "", max_attempts: int = 2
    ) -> bool:
        """Try to rebuild the WebSocket transport within the same adapter instance.

        This keeps the adapter alive so in-flight agent sessions can continue
        sending responses through the same adapter reference, avoiding the
        "transport not connected" failure caused by gateway adapter replacement.
        """
        if (
            self._agent_deleted
            or self._disconnect_requested
            or getattr(self, "_shutting_down", False)
        ):
            return False
        async with self._reconnect_lock:
            # A status callback may have waited for another reconnect or for
            # adapter shutdown. Re-check terminal state after acquiring the
            # lock so stale queued callbacks cannot resurrect the transport.
            if (
                self._agent_deleted
                or self._disconnect_requested
                or getattr(self, "_shutting_down", False)
            ):
                return False

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
                if (
                    self._agent_deleted
                    or self._disconnect_requested
                    or getattr(self, "_shutting_down", False)
                ):
                    return False
                try:
                    new_client = self._new_primary_transport_client()
                    self._bind_packet_handler(new_client)
                    await new_client.connect()
                    self._client = new_client
                    self._mark_connected()
                    await self._report_skills()
                    # 补发滞留的 event_result。重连回调不在 packet handler scope 内，
                    # 必须显式把新主连接放进 ContextVar —— 否则下游 _active_client()
                    # 取不到 client 而整批放弃补发（共享子连接重连路径同此写法）。
                    token = _CURRENT_CLIENT_CTX.set(new_client)
                    try:
                        await self._replay_pending_completed_events()
                        # 重连成功补推队列快照（对齐 connector onReconnected）：
                        # 断连期间的队列变化前端收不到，靠这次全量覆盖对齐。
                        await self._push_all_queue_snapshots()
                    finally:
                        _CURRENT_CLIENT_CTX.reset(token)
                    logger.info(
                        "[%s] Internal reconnect OK (attempt %d)",
                        self.name,
                        attempt,
                    )
                    return True
                except GrixAuthRejectedError as exc:
                    # 只有明确的永久错误才终止重连。10001 可能出现在服务端故障恢复窗口，
                    # 若把它标成 non-retryable，Hermes watcher 会永久移除该连接。
                    if exc.code == AUTH_CODE_AGENT_DELETED:
                        self._agent_deleted = True
                        logger.error(
                            "[%s] Agent 已在平台删除（auth_ack %d），终止重连: %s",
                            self.name,
                            AUTH_CODE_AGENT_DELETED,
                            exc,
                        )
                        self._set_fatal_error("grix_agent_deleted", str(exc), retryable=False)
                        return False

                    logger.warning(
                        "[%s] Internal reconnect auth rejected (attempt %d/%d), "
                        "treating as retryable: %s",
                        self.name,
                        attempt,
                        max_attempts,
                        exc,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] Internal reconnect attempt %d/%d failed: %s",
                        self.name,
                        attempt,
                        max_attempts,
                        exc,
                    )

                if attempt < max_attempts:
                    delay = _reconnect_delay_seconds(attempt)
                    logger.info(
                        "[%s] Internal reconnect retry in %.1fs",
                        self.name,
                        delay,
                    )
                    await asyncio.sleep(delay)

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
        self._client = self._new_primary_transport_client()
        self._bind_packet_handler(self._client)
        try:
            await self._client.connect()
        except GrixAuthRejectedError as exc:
            if exc.code == AUTH_CODE_AGENT_DELETED:
                # agent 已在平台删除：fatal，永久停止重连（与 connector 行为一致）。
                self._agent_deleted = True
                logger.error(
                    "[%s] Agent 已在平台删除（auth_ack %d），停止重连: %s",
                    self.name,
                    AUTH_CODE_AGENT_DELETED,
                    exc,
                )
                self._set_fatal_error("grix_agent_deleted", str(exc), retryable=False)
            else:
                # Generic auth failures can be transient while the service is
                # recovering. Keep the gateway watcher retry queue alive; its
                # capped backoff controls long-running retry frequency.
                logger.warning(
                    "[%s] Auth rejected during connect; keeping background "
                    "reconnect enabled: %s",
                    self.name,
                    exc,
                )
                self._set_fatal_error("grix_auth_rejected", str(exc), retryable=True)
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
        await self._start_skill_syncer()
        self._ensure_provider_quota_refresh()
        return True

    async def _report_skills(
        self,
        *,
        force: bool = True,
        cwd: Optional[str] = None,
        raise_on_error: bool = False,
    ) -> bool:
        """扫描本地 skills + library_skills 并通过 agent_skills_update 上报。

        force=True（连接/重连/同步成功/enable-disable）：无条件上报。
        force=False（会话活动触发）：仅当清单相对上次上报发生变化时才推，避免刷屏。
        cwd：会话绑定工作目录；project scope 完全依赖它，禁止 os.getcwd 兜底。
        raise_on_error：扫描异常时透传给调用方（默认吞掉保 best-effort）；下拉刷新
        （skill_refresh）必须置 True——错误路径不能对用户谎报刷新成功。
        返回是否真正推出了 agent_skills_update。"""
        try:
            from .exec_command import scan_hermes_skills
            from .library_skills import list_library_skills
            from .skill_paths import resolve_library_skills_dir
            from .skill_sync_state import annotate_sync_states

            entries = scan_hermes_skills()
            # sync_state 对照库台账（~/.grix/skills），不是启用根。
            skills = annotate_sync_states(entries, resolve_library_skills_dir())
            library = list_library_skills(cwd=cwd)
            if not skills and not library:
                return False
            digest = json.dumps(
                {
                    "skills": [
                        f"{s['source']}:{s['name']}:{s.get('sync_state')}" for s in skills
                    ],
                    "library": [
                        f"{s['name']}:{s.get('enable_scopes')}" for s in library
                    ],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if not force and digest == self._last_skills_hash:
                return False
            self._last_skills_hash = digest
            if self._client:
                # 启动时主连接的管理性主动调用,不在 packet handler 上下文,显式走主连接。
                await self._client.send_skills_update(skills, library_skills=library)
                logger.info(
                    "[%s] Reported %d skill(s), %d library skill(s)",
                    self.name,
                    len(skills),
                    len(library),
                )
                return True
            return False
        except Exception as exc:
            logger.debug("[%s] Skills report failed: %s", self.name, exc)
            if raise_on_error:
                raise
            return False

    async def disconnect(self) -> None:
        self._disconnect_requested = True
        sweep = getattr(self, "_bg_hold_sweep_task", None)
        if sweep is not None:
            sweep.cancel()
            self._bg_hold_sweep_task = None
        quota_task = getattr(self, "_provider_quota_task", None)
        if quota_task is not None:
            quota_task.cancel()
            self._provider_quota_task = None
        if self._upgrade_checker:
            self._upgrade_checker.stop()
            self._upgrade_checker = None
        # getattr 防御：部分测试用 __new__ 构造 adapter 跳过 __init__（同 _event_queue 先例）。
        skill_syncer = getattr(self, "_skill_syncer", None)
        if skill_syncer:
            skill_syncer.stop()
            self._skill_syncer = None
        # agent 共享：置位 shutting_down,串行等在途 share-set 同步结束,避免关停后泄漏。
        self._shutting_down = True
        getattr(self, "_desired_shared_owner_ids", set()).clear()
        shared_reconnect_tasks = list(
            getattr(self, "_shared_reconnect_tasks", {}).values()
        )
        getattr(self, "_shared_reconnect_tasks", {}).clear()
        for task in shared_reconnect_tasks:
            task.cancel()
        if shared_reconnect_tasks:
            await asyncio.gather(*shared_reconnect_tasks, return_exceptions=True)
        # 清空事件队列：排队事件已 ack 给平台，静默丢弃会留下永远 running 的
        # 幽灵任务（对齐 connector removeSlot：destroy 前先逐条以 canceled
        # 收口）。此时主连接还活着，终态能发出去。
        queue = getattr(self, "_event_queue", None)
        if queue is not None:
            for item in queue.drain_all_queued():
                try:
                    await self._complete_event_if_needed(
                        item.event_id,
                        status=STATUS_CANCELED,
                        message="canceled by shutdown",
                    )
                except Exception as exc:
                    logger.debug(
                        "[%s] shutdown queue settle failed for %s: %s",
                        self.name, item.event_id, exc,
                    )
            queue.destroy()
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
        is_final_reply: bool = False,
    ) -> SendResult:
        # 引用收敛（对齐 connector 语义）：过程消息一律不带引用——服务端把
        # 「agent 引用另一 agent 的消息」视为隐式 @ 并触发对方接活，流式过程里每条
        # 带引用的消息都会重复误触发。最终应答由 grix_reply 工具走 force_quote=True
        # 显式补引用；reply_to 仍保留用于 busy-ack 跟踪等内部匹配。
        client = await self._get_ready_client(operation="send")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)

        # Resolve routing metadata before any wire-only tool-card compaction.
        # Tool cards discard provider metadata, but must remain in the same
        # conversation thread as the original progress event.
        thread_id_hint = self._metadata_thread_id(metadata)

        # Read per-session connector hints injected by the backend (e.g. group chat).
        state = self._active_state()
        _hints = state.session_connector_hints.get(str(chat_id)) or {}
        _drop_thinking = _hints.get("thinking_events") == "drop"
        _drop_tools = _hints.get("tool_events") == "drop"
        # 托管代答场景（后端对 widget 客服等私聊托管下发）：agent 代 owner 回复对端，
        # 只有 grix_reply 的正式应答（is_final_reply=True）该给对端看；经 send() 的
        # 纯文本过程/续写一律不投递。按调用入口判定，不依赖引用/force_quote——长回复
        # 分片仅首片带引用、第二次 grix_reply 也不带引用，那些信号都是有损的。
        _drop_text = _hints.get("text_events") == "drop"
        # 框架整轮最终应答兜底：base.py "Sending response" 路径投递前会在 metadata
        # 打 notify=True 标记（仅最终应答/语音应答打标，过程文本不带）。若同一触发
        # 消息已经由 grix_reply 成功投递，框架 final 只是工具调用后的重复总结，必须
        # 静默收口；显式第二次 grix_reply 走 is_final_reply=True，不受影响。按原消息
        # ID 匹配，避免上一轮 replied 状态误伤同一会话的新消息。
        _is_framework_final = (
            not is_final_reply and (metadata or {}).get("notify") is True
        )
        reply_to_id = str(reply_to or "").strip()
        if _is_framework_final and reply_to_id:
            for target in state.active_reply_targets.values():
                if not target.get("replied"):
                    continue
                if str(target.get("chat_id") or "") != str(chat_id):
                    continue
                if str(target.get("message_id") or "") != reply_to_id:
                    continue
                logger.debug(
                    "[%s] Suppressing framework final after grix_reply "
                    "for chat=%s message_id=%s",
                    self.name,
                    chat_id,
                    reply_to_id,
                )
                return SendResult(success=True, retryable=False)

        # grix_reply 完成最终应答后，模型在同一轮里继续输出的纯文本（流式文本
        # 通道，不带 notify 标记、常无 reply_to）只是重复总结，必须同样收口——
        # 线上实证：gateway.run 的 final-send 去重只挡 notify=True 路径，流式
        # 文本会绕过它直接投递成第二条重复消息。按处理任务 context 里的
        # session_key 精确定位本轮 entry（群聊 per-user 并发时 chat_id 无法
        # 消歧）；entry 在 on_processing_complete 才被清除，窗口正好覆盖
        # 「replied 之后到本轮结束」。ContextVar 缺失（非处理任务链路）时宁可
        # 放过，不误伤其它轮次/其它用户的文本。
        if not is_final_reply and not _is_framework_final:
            _ctx_key = _CURRENT_REPLY_SESSION_KEY.get()
            _target = (
                state.active_reply_targets.get(_ctx_key) if _ctx_key else None
            )
            if (
                _target
                and _target.get("replied")
                and str(_target.get("chat_id") or "") == str(chat_id)
            ):
                logger.debug(
                    "[%s] Dropping post-reply text after grix_reply "
                    "for chat=%s session_key=%s (%d chars)",
                    self.name,
                    chat_id,
                    _ctx_key,
                    len(content or ""),
                )
                return SendResult(success=True, retryable=False)

        # 托管场景下模型未走 grix_reply 时，最终应答也经普通 send() 到达这里——
        # 必须按最终应答对待（跳过过程分类与 text drop），否则对端完全收不到回复，
        # 且静默 success 会让框架误判已投递。
        if _drop_text and _is_framework_final:
            is_final_reply = True

        # Detect structured content and inject channel_data for card display.
        # Order matters: a gateway status line is checked first and short-circuits,
        # so it is never routed to the tool_execution path.  (Today's tool-progress
        # regex doesn't match these strings anyway, but ordering keeps the two
        # classifiers independent of future regex changes.)
        # 正式应答（is_final_reply=True）无条件按普通消息投递：跳过 status/tool/hook
        # 分类，避免正文恰好命中这些窄正则时被误判成过程卡片、或（托管/群聊 drop 时）
        # 被当过程内容丢弃。分类仅对非最终的过程输出生效。
        tp = None  # set only on the tool-progress path
        is_tool_execution = False
        status_text = detect_agent_status(content) if not is_final_reply else None
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
            tp = detect_tool_progress(content) if not is_final_reply else None
            if tp:
                if _drop_tools:
                    # Backend instructed us to suppress tool execution events.
                    return SendResult(success=True, retryable=False)
                tool_name, preview = tp
                is_tool_execution = True
                # The original progress content and metadata belong to the
                # local Hermes transcript/audit path.  Build a new, minimal
                # wire-only payload so verbose args, provider blobs and tool
                # output do not cross the websocket boundary.
                content = ""
                metadata = {
                    "channel_data": build_tool_execution_channel_data(tool_name, preview),
                }
            else:
                hs = detect_hook_status(content) if not is_final_reply else None
                if hs:
                    if _drop_tools:
                        return SendResult(success=True, retryable=False)
                    action_name, description = hs
                    is_tool_execution = True
                    content = ""
                    metadata = {
                        "channel_data": build_tool_execution_channel_data(
                            action_name,
                            description,
                        ),
                    }
                elif _drop_text and not is_final_reply:
                    # 纯文本过程消息/续写（非 status/tool/hook 卡片）：托管场景下非最终
                    # 应答一律不投递给对端。grix_reply 走 send_final_reply
                    # (is_final_reply=True)，不受影响。
                    logger.warning(
                        "[%s] Dropping intermediate text (%d chars) to %s: text_events=drop",
                        self.name,
                        len(content),
                        chat_id,
                    )
                    return SendResult(success=True, retryable=False)

        # 流式 preview 缓冲：Hermes gateway 在 proxy 等路径下即使
        # SUPPORTS_MESSAGE_EDITING=False 仍会跑 GatewayStreamConsumer；它会先
        # send 一条 expect_edits=True 的 preview 消息，再反复 edit_message 更新。
        # Grix 协议没有客户端编辑能力，preview 会凝固成「不完整消息气泡」留在对端。
        # 这里把 preview 帧缓冲在 adapter 内，仅在 finalize 时一次性发出完整内容。
        _is_stream_preview = (
            (metadata or {}).get("expect_edits") is True
            and not is_final_reply
            and (metadata or {}).get("notify") is not True
        )
        if _is_stream_preview:
            self._clean_stale_streaming_previews(state)
            fake_id = f"__grix_stream_preview:{chat_id}"
            state.streaming_previews[str(chat_id)] = {
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
                "fake_message_id": fake_id,
                "updated_at": time.monotonic(),
            }
            logger.debug(
                "[%s] Buffering streaming preview for chat=%s (%d chars)",
                self.name,
                chat_id,
                len(content or ""),
            )
            return SendResult(success=True, message_id=fake_id, retryable=False)

        # 最终/兜底发送时清掉同 chat 的过时 preview buffer，避免残留内容被下一轮的
        # edit_message(finalize=True) 误发。
        if is_final_reply or (metadata or {}).get("notify") is True:
            state.streaming_previews.pop(str(chat_id), None)

        await self._enforce_send_rate()

        source_hint = self._active_state().latest_sources.get(str(chat_id))
        session_id, thread_id = await resolve_grix_target(
            client,
            self.connection,
            str(chat_id),
            thread_id=thread_id_hint,
            source_hint=source_hint,
        )
        # NOTE: event_id is deliberately NOT included in send_text here.
        # Previously, the first streaming chunk carried event_id and called
        # _complete_event_if_needed, which closed the backend pending event
        # prematurely.  Subsequent final-response sends then hit 4003
        # "event_id not owned by current agent".
        # Event lifecycle is now managed at the handler level
        # (_handle_message_packet) instead.
        biz_card = (
            None
            if is_tool_execution
            else _clone_metadata_object(metadata, "biz_card")
        )
        channel_data = _clone_metadata_object(metadata, "channel_data")

        try:
            if is_tool_execution:
                # A tool card is fully represented by channel_data.  Keep one
                # empty send so no raw progress line is duplicated in content.
                chunks = [""]
            else:
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
            if is_tool_execution and result.success and result.message_id:
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
                is_final_reply=True,
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
            # 开放式提问是发给对端的正常消息（非过程文本），托管场景也必须投递。
            return await self.send(
                str(chat_id), f"❓ {question}", metadata=metadata, is_final_reply=True
            )

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

        # 流式 preview 更新 / 收口：GatewayStreamConsumer 先 send preview 拿到 fake id，
        # 之后用 edit_message 更新。非 finalize 时仅更新 buffer；finalize 时一次性发出。
        state = self._active_state()
        if str(message_id).startswith("__grix_stream_preview:"):
            self._clean_stale_streaming_previews(state)
            preview = state.streaming_previews.get(str(chat_id))
            if not finalize:
                if preview is not None:
                    preview["content"] = content
                    preview["updated_at"] = time.monotonic()
                return SendResult(success=True, message_id=message_id, retryable=False)
            # finalize=True: 把 stream consumer 传来的最终内容一次性发出（带引用触发消息）。
            # 注意用调用方传入的 content（ accumulated 完整文本），而不是 buffer 里最后一次
            # 更新的中间内容。
            state.streaming_previews.pop(str(chat_id), None)
            reply_to = preview.get("reply_to") if preview is not None else None
            flush_metadata: Optional[Dict[str, Any]] = None
            if preview is not None and preview.get("metadata"):
                flush_metadata = dict(preview["metadata"])
                flush_metadata.pop("expect_edits", None)
            logger.debug(
                "[%s] Flushing streaming preview for chat=%s (%d chars)",
                self.name,
                chat_id,
                len(content or ""),
            )
            return await self.send(
                str(chat_id),
                content,
                reply_to=reply_to,
                metadata=flush_metadata,
                force_quote=True,
                is_final_reply=True,
            )

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
        # 事件收口后不再点亮 composing（对齐 connector：event_result 上报即停止续期）。
        # hermes 的"正在输入"心跳按 chat 起、只在所属后台任务收尾时取消；同一会话多轮
        # 并发（群里不同发起人各一路）时可能残留一个没人取消的空转循环，它会每 2s 把
        # composing 续起来，任务早已结束前端却一直转。事件收口状态是唯一可信的
        # "还在干活"判据，用它兜住空转循环。
        if not self._session_has_unsettled_events(str(chat_id).split(":")[0]):
            return
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
        if raw_message.get("_grix_kind") not in ("message", "card_action"):
            return
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        state = self._active_state()
        owner_key = self._active_owner_key()
        prior_toolbar = state.toolbar_active_work.get(session_key)
        message_id = str(event.message_id or "").strip()
        if message_id and state.processing_message_ids.get(session_key) == message_id:
            state.processing_message_ids.pop(session_key, None)
        _reply_target = state.active_reply_targets.get(session_key)
        if _reply_target and (not message_id or _reply_target.get("message_id") == message_id):
            state.active_reply_targets.pop(session_key, None)

        # 本轮结束，清掉本 chat 可能残留的流式 preview buffer，避免跨轮次误发。
        state.streaming_previews.pop(str(event.source.chat_id), None)

        raw_event_id = str(raw_message.get("event_id") or "").strip()

        # 本轮任务收口的事件集：on_processing_start 认领的全部事件（含被合并进
        # 同一轮的排队事件）+ 触发事件本身（认领缺失时兜底）。
        event_ids = state.session_running_event_ids.pop(session_key, [])
        if raw_event_id and raw_event_id not in event_ids:
            event_ids.append(raw_event_id)

        is_success = outcome == ProcessingOutcome.SUCCESS or outcome is True
        is_cancelled = outcome == ProcessingOutcome.CANCELLED
        session_id = str(
            (prior_toolbar or {}).get("session_id")
            or getattr(event.source, "chat_id", "")
            or ""
        ).strip()
        # 必须在任何会推 queue_snapshot 的 complete 之前决定保活：
        # _complete_event_if_needed → queue.complete 会立刻推快照；若先 pop
        # 虚拟项，会先发出 running=[]，正是本次要消灭的空窗。
        # 用户主动取消不保活（停止语义优先）。对齐 connector：只保 queue
        # 虚拟项 + composing，不碰 chat_state。
        want_bg_hold = (
            not is_cancelled
            and bool(session_id)
            and self._session_has_bg_hold(session_key)
        )
        if want_bg_hold:
            title = (
                self._bg_hold_label(session_key)
                or (prior_toolbar or {}).get("title")
                or "Background task in progress"
            )
            state.toolbar_active_work[session_key] = {
                "session_id": session_id,
                "title": title,
                "bg_hold": True,
            }
            toolbar_work = None
        else:
            toolbar_work = state.toolbar_active_work.pop(session_key, None)

        # 触发消息被撤回：与 connector 对齐，以 canceled/revoked 终态收口
        # （不能静默不报——后端 durable run 需要终态），同轮其他事件正常收口。
        if message_id and self.is_message_revoked(session_key, message_id):
            state.revoked_message_keys.discard((session_key, message_id))
            if raw_event_id:
                event_ids = [eid for eid in event_ids if eid != raw_event_id]
                await self._complete_event_if_needed(
                    raw_event_id, status=STATUS_CANCELED, message="revoked",
                )
            logger.debug(
                "[%s] Completed revoked GRIX message %s/%s as canceled",
                self.name,
                event.source.chat_id,
                message_id,
            )

        if is_success:
            status, message = STATUS_RESPONDED, None
        elif is_cancelled:
            # 主动停止（/stop、event_stop、queue_clear）触发的预期取消，
            # 与 connector 一致上报 canceled 而非 failed。
            status, message = STATUS_CANCELED, "stopped by user"
        else:
            status, message = STATUS_FAILED, "message processing failed"
        for eid in event_ids:
            await self._complete_event_if_needed(eid, status=status, message=message)

        # 扫尾：会话已无排队消息时，收口本轮内被旁路消化、未进入后台任务的
        # 事件（clarify 文本答复、/approve 等旁路命令）——否则它们无人认领。
        # 这些事件本身被成功消化了，按自身结果报 responded，不跟随本轮结局；
        # 仍在派发途中（inflight）的事件除外：它们可能马上入队/被下一轮认领。
        if not self._session_has_queued_work(session_key):
            inflight = self._inflight_dispatch_event_ids.get(session_key) or ()
            remaining = state.session_open_event_ids.pop(session_key, [])
            deferred = [eid for eid in remaining if eid in inflight]
            if deferred:
                state.session_open_event_ids[session_key] = deferred
            for eid in remaining:
                if eid not in inflight and eid not in event_ids:
                    await self._complete_event_if_needed(eid, status=STATUS_RESPONDED)

        if want_bg_hold and session_id:
            # 强制再推一次：更新虚拟标题（进程 command），避免前端短暂停在 start 文案。
            await self._push_queue_snapshot(session_id, owner_key)
            await self._refresh_bg_hold_activity(session_id, owner_key, active=True)
            self._ensure_bg_hold_sweep()
            return

        # 显式队列事件的收口会自行推快照；旁路/自驱轮次不一定在 EventQueue
        # 中，因此无条件再推一次最终权威快照，清掉虚拟 running 项。
        if toolbar_work is not None:
            await self._push_queue_snapshot(toolbar_work["session_id"], owner_key)

    async def on_processing_start(self, event: MessageEvent) -> None:
        raw_message = event.raw_message if isinstance(event.raw_message, dict) else {}
        if raw_message.get("_grix_kind") not in ("message", "card_action"):
            return
        session_key = build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        # 认领本轮任务要收口的事件：触发事件本身 + 排队消费时移交给本轮的事件
        # （next_run，含被合并后 event_id 丢失的排队消息）。不抓取其他 open
        # 事件——它们可能属于未来轮次（刚入队/防抖中），提前认领会提前收口。
        # on_processing_complete 按任务真实结果统一发 event_result。
        state = self._active_state()
        owner_key = self._active_owner_key()
        running = state.session_next_run_event_ids.pop(session_key, [])
        raw_event_id = str(raw_message.get("event_id") or "").strip()
        if raw_event_id and raw_event_id not in running:
            running.append(raw_event_id)
        for eid in running:
            self._discard_open_event(session_key, eid)
        if running:
            # 合并而不是覆盖：上一轮若异常退出未走完 complete 钩子，残留的
            # 认领并入本轮一起收口，避免被覆盖后永久悬挂。
            existing = state.session_running_event_ids.setdefault(session_key, [])
            for eid in running:
                if eid not in existing:
                    existing.append(eid)

        # 对齐 grix-connector 的 selfDrivenSessions：Hermes 的框架轮次可能由
        # 斜杠命令、pending 合并或内部续跑触发，此时显式 EventQueue 未必能
        # 提供 running 项。记录真实处理态，让队列快照合成一个虚拟任务兜底。
        session_id = str(event.source.chat_id or "").strip()
        if session_id:
            state.toolbar_active_work[session_key] = {
                "session_id": session_id,
                "title": build_preview(getattr(event, "text", "")),
            }
            await self._push_queue_snapshot(session_id, owner_key)

        if raw_message.get("_grix_kind") != "message" or not event.message_id:
            return
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

    def _new_shared_transport_client(
        self,
        shared_owner_id: str,
    ) -> GrixTransportClient:
        """Build a source-aware shared client."""
        shared_config = build_shared_connection_config(
            self.connection,
            shared_owner_id,
        )
        client = GrixTransportClient(
            shared_config,
            connector=self._connector,
            on_status=None,
        )
        client.on_status = self._make_shared_status_handler(
            shared_owner_id,
            source_client=client,
        )
        return client

    def _make_shared_status_handler(
        self,
        shared_owner_id: str,
        *,
        source_client: Optional[GrixTransportClient] = None,
    ) -> Callable:
        """Return an on_status callback bound to a specific shared_owner_id.

        When the shared client disconnects unexpectedly, this triggers
        automatic reconnection — mirroring _handle_transport_status for
        the primary client."""

        async def handler(status: Dict[str, Any]) -> None:
            if self._disconnect_requested or self._shutting_down:
                return
            if status.get("connected", True):
                return
            if (
                source_client is not None
                and self._shared_clients.get(shared_owner_id) is not source_client
            ):
                logger.debug(
                    "[%s] Ignoring stale shared transport status callback "
                    "shared_owner=%s",
                    self.name,
                    shared_owner_id,
                )
                return
            reason = str(status.get("last_error") or "shared client disconnected")
            self._ensure_shared_reconnect_task(shared_owner_id, reason=reason)

        return handler

    def _ensure_shared_reconnect_task(
        self,
        shared_owner_id: str,
        *,
        reason: str = "",
    ) -> Optional[asyncio.Task]:
        if self._disconnect_requested or self._shutting_down or self._agent_deleted:
            return None
        desired = getattr(self, "_desired_shared_owner_ids", set())
        if shared_owner_id not in desired:
            return None

        tasks = getattr(self, "_shared_reconnect_tasks", None)
        if tasks is None:
            tasks = self._shared_reconnect_tasks = {}
        existing = tasks.get(shared_owner_id)
        if existing is not None and not existing.done():
            return existing

        client = self._shared_clients.get(shared_owner_id)
        status = getattr(client, "status", None) if client is not None else None
        if (
            isinstance(status, dict)
            and status.get("connected")
            and status.get("authed")
        ):
            return None

        task = asyncio.create_task(
            self._try_reconnect_shared_client(
                shared_owner_id,
                reason=reason,
                max_attempts=None,
            )
        )
        tasks[shared_owner_id] = task

        def _done(done_task: asyncio.Task) -> None:
            if tasks.get(shared_owner_id) is done_task:
                tasks.pop(shared_owner_id, None)
            if done_task.cancelled():
                return
            try:
                error = done_task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                logger.error(
                    "[%s] Shared reconnect task crashed shared_owner=%s: %s",
                    self.name,
                    shared_owner_id,
                    error,
                    exc_info=(type(error), error, error.__traceback__),
                )

        task.add_done_callback(_done)
        return task

    async def _try_reconnect_shared_client(
        self,
        shared_owner_id: str,
        *,
        reason: str = "",
        max_attempts: Optional[int] = 2,
    ) -> bool:
        """Reconnect a shared client with cancellable, capped backoff.

        ``max_attempts=None`` keeps retrying until the share is revoked or the
        adapter shuts down. A finite value remains available for narrow tests.
        Generic auth rejection is retryable because 10001 is also emitted while
        the service is recovering; only explicit agent deletion is terminal.
        """
        attempt = 0
        first_reason = reason or "shared client disconnected"

        while True:
            if self._shutting_down or self._disconnect_requested:
                return False

            async with self._share_sync_lock:
                desired = getattr(self, "_desired_shared_owner_ids", set())
                if shared_owner_id not in desired:
                    return False
                old_client = self._shared_clients.get(shared_owner_id)
                status = (
                    getattr(old_client, "status", None)
                    if old_client is not None
                    else None
                )
                if (
                    isinstance(status, dict)
                    and status.get("connected")
                    and status.get("authed")
                ):
                    return True
                self._shared_clients.pop(shared_owner_id, None)

            if old_client is not None:
                with suppress(Exception):
                    await old_client.disconnect(first_reason)

            attempt += 1
            new_client = self._new_shared_transport_client(shared_owner_id)
            self._bind_packet_handler(new_client)
            try:
                await new_client.connect()
            except asyncio.CancelledError:
                with suppress(Exception):
                    await new_client.disconnect("shared reconnect cancelled")
                raise
            except GrixAuthRejectedError as exc:
                if exc.code == AUTH_CODE_AGENT_DELETED:
                    self._agent_deleted = True
                    logger.error(
                        "[%s] Shared reconnect stopped: agent deleted "
                        "shared_owner=%s: %s",
                        self.name,
                        shared_owner_id,
                        exc,
                    )
                    self._set_fatal_error(
                        "grix_agent_deleted",
                        str(exc),
                        retryable=False,
                    )
                    await self._notify_fatal_error()
                    return False
                logger.warning(
                    "[%s] Shared reconnect auth rejected (attempt %d) "
                    "shared_owner=%s, treating as retryable: %s",
                    self.name,
                    attempt,
                    shared_owner_id,
                    exc,
                )
            except Exception as exc:
                logger.warning(
                    "[%s] Shared reconnect attempt %d failed "
                    "shared_owner=%s: %s",
                    self.name,
                    attempt,
                    shared_owner_id,
                    exc,
                )
            else:
                install_client = False
                async with self._share_sync_lock:
                    desired = getattr(self, "_desired_shared_owner_ids", set())
                    if (
                        shared_owner_id in desired
                        and not self._shutting_down
                        and not self._disconnect_requested
                    ):
                        self._shared_clients[shared_owner_id] = new_client
                        install_client = True

                if not install_client:
                    with suppress(Exception):
                        await new_client.disconnect("share revoked during reconnect")
                    return False

                logger.info(
                    "[%s] Shared client reconnect OK shared_owner=%s (attempt %d)",
                    self.name,
                    shared_owner_id,
                    attempt,
                )
                # 补发断连期间滞留的 event_result（按该 owner 的状态桶）。
                token = _CURRENT_CLIENT_CTX.set(new_client)
                try:
                    await self._replay_pending_completed_events()
                    await self._push_all_queue_snapshots()
                except Exception as exc:
                    logger.debug(
                        "[%s] Shared client event_result replay failed "
                        "shared_owner=%s: %s",
                        self.name,
                        shared_owner_id,
                        exc,
                    )
                finally:
                    _CURRENT_CLIENT_CTX.reset(token)
                return True

            if max_attempts is not None and attempt >= max_attempts:
                logger.error(
                    "[%s] Shared client reconnect failed after %d attempts "
                    "shared_owner=%s",
                    self.name,
                    attempt,
                    shared_owner_id,
                )
                return False

            delay = (
                _background_reconnect_delay_seconds(attempt)
                if max_attempts is None
                else _reconnect_delay_seconds(attempt)
            )
            logger.info(
                "[%s] Shared reconnect retry in %.1fs shared_owner=%s",
                self.name,
                delay,
                shared_owner_id,
            )
            await asyncio.sleep(delay)

    async def _handle_transport_status(
        self,
        status: Dict[str, Any],
        *,
        source_client: Optional[GrixTransportClient] = None,
    ) -> None:
        if self._disconnect_requested or self._agent_deleted or self._shutting_down:
            return
        if status.get("connected", True):
            return
        if not self.is_connected:
            return

        status_lock = getattr(self, "_status_reconnect_lock", None)
        if status_lock is None:
            # Compatibility for narrow tests that construct the adapter via
            # __new__ instead of __init__.
            status_lock = self._status_reconnect_lock = asyncio.Lock()

        # Cover both the internal rebuild and the gateway fatal handoff. This
        # coalesces duplicate on_status callbacks rather than merely serializing
        # each callback into another full reconnect cycle.
        async with status_lock:
            if self._disconnect_requested or self._agent_deleted or self._shutting_down:
                return
            if source_client is not None and source_client is not self._client:
                logger.debug(
                    "[%s] Ignoring stale primary transport status callback",
                    self.name,
                )
                return
            if status.get("connected", True) or not self.is_connected:
                return

            message = str(status.get("last_error") or "grix websocket disconnected")

            # Try internal transport reconnection first — keeps the same adapter
            # instance alive so in-flight agent sessions can still send responses.
            if await self._try_reconnect_transport(reason=message):
                return

            if self._disconnect_requested or self._agent_deleted or self._shutting_down:
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
            if cmd in (CMD_EVENT_MSG, CMD_EVENT_STOP) and source_client is not None:
                # Persist negotiated terminal tokens before exposing the event
                # to bridge logic (fail-closed: close socket on persist failure).
                if not source_client.capture_inbound_terminal_commit_token(
                    payload.get("event_id"),
                    payload.get("terminal_commit_token"),
                ):
                    return
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
            elif cmd == CMD_QUEUE_REORDER:
                await self._handle_queue_reorder_packet(payload)
            elif cmd == CMD_EVENT_HOLD:
                await self._handle_event_hold_packet(payload)
            elif cmd == CMD_QUEUE_EDIT:
                await self._handle_queue_edit_packet(payload)
            elif cmd == CMD_QUEUE_SNAPSHOT_QUERY:
                await self._handle_queue_snapshot_query_packet(payload)
            elif cmd == CMD_KICKED:
                await self._handle_kicked_packet(payload, source_client)
            elif cmd == CMD_SKILL_SYNC:
                # 技能库变更提醒：立即触发本机下拉同步（轮询兜底仍在）。
                logger.info(
                    "[%s] skill_sync received owner=%s name=%s",
                    self.name,
                    payload.get("owner_id", ""),
                    payload.get("name", ""),
                )
                if self._skill_syncer:
                    self._skill_syncer.trigger_sync()
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

    async def _handle_kicked_packet(
        self,
        payload: Dict[str, Any],
        source_client: Optional[GrixTransportClient],
    ) -> None:
        """服务端 kicked 通知。reason=agent_deleted 时按 fatal 处理：
        agent 已在平台删除，停止整个 adapter 并永久禁止重连（与 connector 行为一致）。
        其他 reason（如连接被替换）不在这里处理——传输层断开后走既有重连路径。"""
        reason = str(payload.get("reason") or "")
        if reason != KICKED_REASON_AGENT_DELETED:
            logger.warning(
                "[%s] kicked by server reason=%s (transport close will follow existing reconnect path)",
                self.name,
                reason or "<none>",
            )
            return
        # 共享子连接收到的删除信号不单独处理：主连接必然也会收到，由主连接统一收口。
        if source_client is not None and self._client is not None and source_client is not self._client:
            logger.warning(
                "[%s] kicked(agent_deleted) on shared client, waiting for primary to handle",
                self.name,
            )
            return
        if self._agent_deleted:
            return
        self._agent_deleted = True
        logger.error(
            "[%s] Agent 已在平台删除（kicked reason=%s），停止连接并永久禁止重连",
            self.name,
            reason,
        )
        self._set_fatal_error("grix_agent_deleted", "agent deleted on platform", retryable=False)
        try:
            await self.disconnect()
        except Exception as exc:
            logger.warning("[%s] disconnect after agent_deleted failed: %s", self.name, exc)
        await self._notify_fatal_error()

    async def _handle_share_set_packet(self, payload: Dict[str, Any]) -> None:
        """agent 共享：后端下发当前被共享者全量名单，diff 后增删共享子连接。
        每个被共享者一条独立 WS（主人 api_key + shared_owner_id），handler 回调
        通过 contextvar 路由到各自 client，确保回执不串。"""
        # Compatibility for narrow tests that construct the adapter via
        # __new__ instead of __init__.
        if not hasattr(self, "_desired_shared_owner_ids"):
            self._desired_shared_owner_ids = set(self._shared_clients.keys())
        if not hasattr(self, "_shared_reconnect_tasks"):
            self._shared_reconnect_tasks = {}

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
            previous = set(self._desired_shared_owner_ids)
            previous.update(self._shared_clients.keys())
            self._desired_shared_owner_ids = set(desired)
            to_remove = previous - desired
            removed_clients = {
                shared_owner_id: self._shared_clients.pop(shared_owner_id, None)
                for shared_owner_id in to_remove
            }
            removed_tasks = {
                shared_owner_id: self._shared_reconnect_tasks.pop(
                    shared_owner_id,
                    None,
                )
                for shared_owner_id in to_remove
            }

        # Cancellation and network shutdown happen outside _share_sync_lock so
        # an in-flight worker can unwind without deadlocking share-set handling.
        tasks_to_cancel = [task for task in removed_tasks.values() if task is not None]
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)

        for shared_owner_id, shared_client in removed_clients.items():
            if shared_client is not None:
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

        # Missing/disconnected desired owners each get one cancellable worker.
        # The worker attempts immediately, then retries indefinitely with
        # capped jittered backoff until a later share-set revokes the owner.
        for shared_owner_id in desired:
            self._ensure_shared_reconnect_task(
                shared_owner_id,
                reason="control_share_set",
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

        if action.action_type == LOCAL_ACTION_CREATE_FOLDER:
            await self._handle_create_folder(action)
            return

        if action.action_type == LOCAL_ACTION_CONNECTOR_UPGRADE_PUSH:
            await self._handle_upgrade_push(action)
            return

        if action.action_type == LOCAL_ACTION_GET_SESSION_USAGE:
            await self._handle_get_session_usage(action)
            return

        if action.action_type == LOCAL_ACTION_GET_RATE_LIMITS:
            await self._handle_get_rate_limits(action)
            return

        if action.action_type == LOCAL_ACTION_SKILL_UPLOAD:
            await self._handle_skill_upload(action)
            return

        if action.action_type == LOCAL_ACTION_SKILL_ENABLE:
            await self._handle_skill_enable(action)
            return

        if action.action_type == LOCAL_ACTION_SKILL_DISABLE:
            await self._handle_skill_disable(action)
            return

        if action.action_type == LOCAL_ACTION_SKILL_REFRESH:
            await self._handle_skill_refresh(action)
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

    async def _handle_create_folder(self, action: GrixLocalAction) -> None:
        from .create_folder import handle_create_folder_action
        from .file_list import real_home_dir

        if not self._active_client():
            return
        result = handle_create_folder_action(
            action.params,
            resolve_cwd=lambda _session_id: None,
            fallback_dir=real_home_dir(),
        )
        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=result["status"],
            result=result.get("result"),
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

    async def _handle_get_rate_limits(self, action: GrixLocalAction) -> None:
        """工具栏点击刷新厂商限额：强制拉一次配额并回写 binding meta。"""
        if not self._active_client():
            return
        source = resolve_provider_quota_source()
        if source is None:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_OK,
                result={
                    "adapterType": "hermes",
                    "available": False,
                    "cached": False,
                    "sampledAt": None,
                    "rateLimits": None,
                    "contextWindow": None,
                    "tokenUsage": None,
                    "providerQuota": None,
                    "error": "provider quota source unavailable",
                },
            )
            return
        try:
            snapshot = await shared_provider_quota_service.query(source, fresh=True)
        except Exception as exc:
            logger.debug("[%s] get_rate_limits query failed: %s", self.name, exc)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_OK,
                result={
                    "adapterType": "hermes",
                    "available": False,
                    "cached": False,
                    "sampledAt": None,
                    "rateLimits": None,
                    "contextWindow": None,
                    "tokenUsage": None,
                    "providerQuota": None,
                    "error": str(exc),
                },
            )
            return
        quota = snapshot["quota"]
        sampled_at = int(snapshot["sampledAt"])
        cached = bool(snapshot.get("cached"))
        if quota.get("success"):
            self._provider_quota = quota
            self._provider_quota_sampled_at_ms = sampled_at
            await self._push_all_queue_snapshots()
            rate_limits = provider_quota_to_rate_limits(quota, sampled_at)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_OK,
                result={
                    "adapterType": "hermes",
                    "available": True,
                    "cached": cached,
                    "sampledAt": sampled_at or None,
                    "rateLimits": rate_limits,
                    "contextWindow": None,
                    "tokenUsage": None,
                    "providerQuota": quota,
                },
            )
            return
        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=STATUS_OK,
            result={
                "adapterType": "hermes",
                "available": False,
                "cached": cached,
                "sampledAt": sampled_at or None,
                "rateLimits": None,
                "contextWindow": None,
                "tokenUsage": None,
                "providerQuota": quota,
                "error": quota.get("error"),
            },
        )

    async def _handle_skill_upload(self, action: GrixLocalAction) -> None:
        """工具栏一键上传技能（docs/architecture/39 §4）。系统托管技能一律拒绝——
        识别地基已在扫描阶段打好 managed 标记，这里直接复用（find_uploadable_skill）。
        上传成功后依赖既有 skill_sync 广播 + SkillSyncer 更新本机台账，不重复维护。
        """
        if not self._active_client():
            return
        from .skill_upload import SkillUploadError, upload_skill

        name = str((action.params or {}).get("name") or "").strip()
        if not name:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code="MISSING_SKILL_NAME",
                error_message="name is required",
            )
            return
        try:
            await upload_skill(name, self.connection.api_key, self.connection.endpoint)
        except SkillUploadError as exc:
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code="SKILL_UPLOAD_FAILED",
                error_message=str(exc),
            )
            return
        except Exception as exc:
            logger.warning("[%s] skill_upload failed name=%s: %s", self.name, name, exc)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code="SKILL_UPLOAD_FAILED",
                error_message=str(exc),
            )
            return
        await self._active_client().send_local_action_result(
            action_id=action.action_id,
            status=STATUS_OK,
            result={"name": name},
        )

    async def _handle_skill_enable(self, action: GrixLocalAction) -> None:
        """把库技能软链到 Hermes 启用主根（对齐 connector skill_enable）。"""
        if not self._active_client():
            return
        from .skill_enable import SkillEnableError, enable_skill

        params = action.params or {}
        name = str(params.get("name") or "").strip()
        scope = str(params.get("scope") or "").strip()
        force_raw = params.get("force")
        force = (
            force_raw
            if force_raw in ("replace_link", "replace_with_link")
            else None
        )
        # project scope 只认会话绑定 cwd；Hermes 当前 resolve_cwd 常为 None → unavailable。
        cwd = None
        try:
            result = await enable_skill(name=name, scope=scope, cwd=cwd, force=force)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_OK,
                result={
                    "name": result["name"],
                    "scope": result["scope"],
                    "path": result["path"],
                    "changed": result["changed"],
                    "enable_state": result["status"],
                    "uninstallable": True,
                },
            )
        except SkillEnableError as exc:
            conflict = (
                exc.code.lower()
                if exc.code in ("CONFLICT", "NEEDS_FORCE", "BLOCKED")
                else None
            )
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=exc.code,
                error_message=str(exc),
                result={"conflict_kind": conflict} if conflict else None,
            )
        except Exception as exc:
            logger.warning("[%s] skill_enable failed name=%s: %s", self.name, name, exc)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code="SKILL_ENABLE_FAILED",
                error_message=str(exc),
            )
        await self._report_skills(force=True, cwd=cwd)

    async def _handle_skill_disable(self, action: GrixLocalAction) -> None:
        """摘掉 enable 建的软链（对齐 connector skill_disable）。"""
        if not self._active_client():
            return
        from .skill_enable import SkillEnableError, disable_skill

        params = action.params or {}
        name = str(params.get("name") or "").strip()
        scope = str(params.get("scope") or "").strip()
        cwd = None
        try:
            result = await disable_skill(name=name, scope=scope, cwd=cwd)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_OK,
                result={
                    "name": result["name"],
                    "scope": result["scope"],
                    "path": result["path"],
                    "removed": result["removed"],
                    "enable_state": "none",
                    "uninstallable": False,
                },
            )
        except SkillEnableError as exc:
            conflict = (
                exc.code.lower()
                if exc.code in ("CONFLICT", "BLOCKED")
                else None
            )
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=exc.code,
                error_message=str(exc),
                result={"conflict_kind": conflict} if conflict else None,
            )
        except Exception as exc:
            logger.warning("[%s] skill_disable failed name=%s: %s", self.name, name, exc)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code="SKILL_DISABLE_FAILED",
                error_message=str(exc),
            )
        await self._report_skills(force=True, cwd=cwd)

    async def _handle_skill_refresh(self, action: GrixLocalAction) -> None:
        """技能弹窗「下拉刷新」（对齐 connector skill_refresh）：force 重扫本地
        skills + library_skills 并先推 agent_skills_update、后回 local_action_result
        ——后端收到结果即基于最新 profile 重建工具栏快照，顺序不能反。
        hermes 会话无工作区绑定概念，cwd 恒为 None（与 skill_enable/disable 一致），
        project scope 由 list_library_skills 判为 unavailable。"""
        if not self._active_client():
            return
        params = action.params or {}
        session_id = str(params.get("session_id") or "").strip()
        try:
            pushed = await self._report_skills(force=True, raise_on_error=True)
            if not pushed:
                # 未推出 agent_skills_update（空集/无连接）：如实回 failed，不能让
                # 用户看到「刷新成功」的旧数据（对齐 connector 同路径）。
                logger.warning(
                    "[%s] skill_refresh produced no report session=%s",
                    self.name,
                    session_id or "-",
                )
                await self._active_client().send_local_action_result(
                    action_id=action.action_id,
                    status=STATUS_FAILED,
                    error_code="SKILL_REFRESH_FAILED",
                    error_message="skill rescan produced no report (scan error or empty skill set)",
                )
                return
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_OK,
                result={"session_id": session_id} if session_id else None,
            )
        except Exception as exc:
            logger.warning("[%s] skill_refresh failed: %s", self.name, exc)
            await self._active_client().send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code="SKILL_REFRESH_FAILED",
                error_message=str(exc),
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
                is_busy=self._gateway_is_busy,
                restart=self._request_gateway_restart,
            )
            await self._upgrade_checker.start()
            logger.info("[%s] Upgrade checker started", self.name)
        except Exception as exc:
            logger.warning("[%s] Failed to start upgrade checker: %s", self.name, exc)

    async def _start_skill_syncer(self) -> None:
        """启动自定义技能下拉同步器（docs/architecture/38）。

        落盘 ~/.grix/skills（与 connector 共用库目录）；同步成功后强制刷新
        skills + library_skills 上报。启动失败不影响主链路。
        """
        try:
            from .skill_syncer import SkillSyncer

            # connect() 重入（宿主重连场景）时先停旧实例，避免双 loop 并行。
            if self._skill_syncer:
                self._skill_syncer.stop()
                self._skill_syncer = None

            async def _on_sync_success() -> None:
                # 对齐 connector forceRefreshSkills：不看 fingerprint。
                await self._report_skills(force=True)

            self._skill_syncer = SkillSyncer(
                endpoint=self.connection.endpoint,
                api_key=self.connection.api_key,
                on_change=_on_sync_success,
            )
            await self._skill_syncer.start()
            # migrate / 首轮 sync 无论成败都强制刷一次：升级后库台账迁完若平台不可达，
            # on_change 不会触发，否则工具栏可能长时间看不到 library_skills。
            await self._report_skills(force=True)
            logger.info("[%s] Skill syncer started", self.name)
        except Exception as exc:
            logger.warning("[%s] Failed to start skill syncer: %s", self.name, exc)

    def _gateway_is_busy(self) -> bool:
        """Whether the hosting gateway has agent runs in flight.

        Same busy notion the gateway's /restart uses for its drain notice;
        mirrors the connector's ``instances.some(busy)``. Never raises — a
        probe failure must not turn a finished upgrade into a reported one.
        """
        try:
            from gateway.run import _gateway_runner_ref

            runner = _gateway_runner_ref()
            return bool(runner and runner._running_agent_count() > 0)
        except Exception:
            return False

    def _request_gateway_restart(self) -> bool:
        """Ask the hosting GatewayRunner for a graceful self-restart.

        Mirrors the gateway's own /restart command: under a service manager
        (systemd) or container the gateway exits with the restart code and the
        manager revives it; otherwise ``detached=True`` spawns a watcher that
        waits for this PID to exit and runs ``hermes gateway restart``. Either
        way the gateway drains in-flight tasks and saves state before exiting.

        Returns True when the process's fate is already settled — restart
        handed to the runner, or a stop/restart is actively draining (a planned
        stop must not be flipped into a revival). Returns False when no runner
        is reachable or the runner refused with no stop actually running (a
        stale one-way restart latch), so the caller falls back to SIGTERM.
        """
        from gateway.run import _gateway_runner_ref

        runner = _gateway_runner_ref()
        if not runner:
            return False
        stop_task = getattr(runner, "_stop_task", None)
        if stop_task is not None and not stop_task.done():
            return True
        via_service = (
            bool(os.environ.get("INVOCATION_ID"))
            or os.path.exists("/.dockerenv")
            or os.path.exists("/run/.containerenv")
        )
        if via_service:
            return bool(runner.request_restart(detached=False, via_service=True))
        return bool(runner.request_restart(detached=True, via_service=False))

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

        # /stop 拦截：后端工具栏停止按钮通过 SendStopText 下发 "/stop" 文本命令。
        # 该事件只带 session_id + sender，没有会话类型，按它拼出的 session_key 一律是
        # dm 形态，在群会话（<ns>:grix:group:<session_id>:<user_id>）里永远对不上。
        # 停止的语义是"停这个会话里该 agent 的活"，所以按 session_id 找出该会话下所有
        # 正在跑的任务逐个停。
        if message.text and message.text.strip().lower() == "/stop":
            stop_keys = self._active_session_keys_for_session(message.session_id)
            logger.info(
                "[%s] GRIX /stop command received event_id=%s session_id=%s stop_keys=%s active_sessions=%s",
                self.name, message.event_id, message.session_id, stop_keys,
                list(self._active_sessions.keys()),
            )
            stopped_any = False
            for stop_key in stop_keys:
                # 多路并发时全停，但只给用户一条停止确认。
                if await self._force_stop_session(
                    source, stop_key, reply_to=message.message_id, notify=not stopped_any,
                ):
                    stopped_any = True
            if self._client:
                await self._complete_event_if_needed(
                    message.event_id, status=STATUS_RESPONDED,
                )
            logger.info(
                "[%s] GRIX /stop command handled event_id=%s stopped=%s",
                self.name, message.event_id, stopped_any,
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

        # 队列前置拦截（网关的同款拦截点在 handle_message 之后，被队列门控
        # 挡住够不着，必须在这里等价复刻）：
        # 1. clarify 文本回答——clarify 阻塞的轮次占着本会话执行槽位，打字
        #    回答若按普通消息入队会排在该轮之后，clarify 只能等到超时。
        #    与网关 run.py 拦截语义一致：非斜杠文本、命中待答 clarify 即解锁
        #    并收口事件（agent 线程恢复后自己产出下一条用户可见消息）。
        text_stripped = (message.text or "").strip()
        if text_stripped and not text_stripped.startswith("/"):
            resolved_clarify = False
            try:
                from tools.clarify_gateway import resolve_text_response_for_session
            except ImportError:
                resolve_text_response_for_session = None
            if resolve_text_response_for_session is not None:
                try:
                    resolved_clarify = bool(
                        resolve_text_response_for_session(session_key, text_stripped)
                    )
                except Exception:
                    logger.exception(
                        "[%s] clarify text-resolve failed event_id=%s session_key=%s",
                        self.name, message.event_id, session_key,
                    )
            if resolved_clarify:
                logger.info(
                    "[%s] GRIX clarify text response intercepted event_id=%s session_key=%s",
                    self.name, message.event_id, session_key,
                )
                await self._complete_event_if_needed(
                    message.event_id, status=STATUS_RESPONDED,
                )
                return

        # 2. 斜杠命令——网关本就支持在轮次运行中处理命令（/status、/new、
        #    /queue、/approve 等），入队会把它们卡在长任务后面，因此绕过
        #    队列直接投递，保持与入队前完全相同的即时命令语义。
        if text_stripped.startswith("/"):
            await self._dispatch_grix_event(message, source, session_key)
            return

        # 消息事件统一先进显式队列（对齐 connector）：同会话已有执行中事件时
        # 排队等待，空闲则立即投递（submit 内同步触发 _on_queue_deliver）。
        # 队满拒绝 / 排队超时 / 取消由队列的 state_change 回调统一收口。
        item = QueueItem(
            event_id=message.event_id,
            session_id=message.session_id,
            group_key=session_key,
            owner_key=self._active_owner_key(),
            preview=build_preview(message.text),
            payload=(message, source, session_key),
            content=message.text or "",
        )
        verdict = self._event_queue.submit(item)
        logger.debug(
            "[%s] GRIX event %s queue submit verdict=%s session_key=%s queued=%d running=%d",
            self.name,
            message.event_id,
            verdict,
            session_key,
            self._event_queue.queued_count,
            self._event_queue.running_count,
        )

    def _session_context_block_once(
        self,
        message: GrixInboundMessage,
        source: Any,
        session_key: str,
    ) -> str:
        """Return the one-shot [system-context] block for a fresh hermes session,
        or "" when this hermes session already got it. The hermes session_id
        rotates on idle/daily auto-reset, so a reset session re-injects."""
        session_id = str(getattr(message, "session_id", "") or "").strip()
        if not session_id:
            return ""
        session_store = getattr(self, "_session_store", None)
        if session_store is None or not hasattr(session_store, "get_or_create_session"):
            return ""
        try:
            entry = session_store.get_or_create_session(source)
        except Exception as exc:
            logger.debug("[%s] session-context store lookup failed: %s", self.name, exc)
            return ""
        if not _is_new_hermes_session(entry):
            return ""
        entry_id = str(getattr(entry, "session_id", "") or "")
        if entry_id and self._session_context_injected.get(session_key) == entry_id:
            return ""
        self._session_context_injected[session_key] = entry_id
        return _render_session_context_block(session_id)

    async def _dispatch_grix_event(
        self,
        message: GrixInboundMessage,
        source: Any,
        session_key: str,
    ) -> None:
        """把一条已获得执行槽位的消息事件投递给 hermes 处理（收口登记 + 兜底）。"""
        event_text = message.text
        session_context = self._session_context_block_once(message, source, session_key)
        if session_context:
            event_text = f"{session_context}\n\n{event_text}" if event_text else session_context
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

        # 登记待收口事件：真正的 event_result 由 on_processing_complete 在
        # 后台任务结束时按真实结果发出（对齐 connector 的回合收口语义）。
        # 注意 handle_message 是"派后台任务/入队后立即返回"的，不能在它返回后
        # 就发 responded——那是任务开始，不是任务结束。
        _open_ids = self._active_state().session_open_event_ids.setdefault(session_key, [])
        if message.event_id not in _open_ids:
            _open_ids.append(message.event_id)
        self._inflight_dispatch_event_ids.setdefault(session_key, set()).add(message.event_id)

        try:
            if session_key not in self._active_sessions and message.message_id:
                self._active_state().processing_message_ids[session_key] = message.message_id
            await self.handle_message(event)
        except Exception as exc:
            self._discard_open_event(session_key, message.event_id)
            # 不以 self._client 为前置条件：内部重连窗口 self._client 可能为
            # None，跳过收口会让事件占用的队列槽位永久泄漏、会话卡死。
            # _complete_event_if_needed 内部先释放槽位再决定能否上报。
            await self._complete_event_if_needed(
                message.event_id,
                status=STATUS_FAILED,
                message=str(exc),
            )
            raise
        finally:
            _inflight = self._inflight_dispatch_event_ids.get(session_key)
            if _inflight is not None:
                _inflight.discard(message.event_id)
                if not _inflight:
                    self._inflight_dispatch_event_ids.pop(session_key, None)

        # 兜底：handle_message 返回后，事件既没被后台任务认领（会话未激活）、
        # 也没有排队等待下一轮，说明它已被同步路径消化且不会再有人收口——
        # 按旧语义立即发 responded，避免后端事件悬挂。同样不看 self._client，
        # 断连窗口也必须释放队列槽位。
        if (
            self._event_still_open(session_key, message.event_id)
            and session_key not in self._active_sessions
            and not self._session_has_queued_work(session_key)
        ):
            self._discard_open_event(session_key, message.event_id)
            await self._complete_event_if_needed(
                message.event_id, status=STATUS_RESPONDED,
            )
        elif (
            self._event_still_open(session_key, message.event_id)
            and session_key in self._pending_messages
        ):
            # 已占 EQ running 槽、却被 busy-handler 打进 pending：若当前轮
            # 已过 late_pending 检查，pending 会永久悬挂。挂一个短恢复任务。
            self._schedule_orphaned_pending_recovery(session_key, message.event_id)

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

        # 排队中（尚未运行）的消息被编辑：文本冻结在 QueueItem.payload 的
        # 入站消息里，就地替换，轮到它执行时用的就是编辑后的内容。
        for queued_item in self._event_queue.queued_items():
            q_payload = queued_item.payload if isinstance(queued_item.payload, tuple) else None
            q_message = q_payload[0] if q_payload else None
            if (
                q_message is not None
                and queued_item.session_id == edit.session_id
                and str(getattr(q_message, "message_id", "")) == edit.message_id
            ):
                updated = dataclasses.replace(
                    q_message,
                    text=edit.text,
                    reply_to_message_id=edit.reply_to_message_id,
                )
                queued_item.payload = (updated, q_payload[1], q_payload[2])
                queued_item.preview = build_preview(edit.text)
                logger.debug(
                    "[%s] Updated queued event %s from GRIX edit for %s/%s",
                    self.name,
                    queued_item.event_id,
                    edit.session_id,
                    edit.message_id,
                )
                await self._push_queue_snapshot(
                    queued_item.session_id, queued_item.owner_key
                )
                return

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

        # 被撤回消息对应的事件从收口登记表移除，改由本处以终态收口
        # （对齐 connector：撤回的事件上报 canceled/revoked，不静默丢弃）。
        revoked_event_id = self._active_state().reply_event_ids.get(
            (revoke.session_id, revoke.message_id)
        )
        # 排队中（尚未运行）的消息被撤回：从显式队列摘除，否则下面的收口只
        # 记了终态、队列仍会在轮到它时把已撤回的消息投给 agent 执行。
        if revoked_event_id:
            removed_item = self._event_queue.remove_queued(revoked_event_id)
            if removed_item is not None:
                logger.info(
                    "[%s] GRIX revoke removed queued event %s for %s/%s",
                    self.name, revoked_event_id, revoke.session_id, revoke.message_id,
                )
                await self._push_queue_snapshot(
                    removed_item.session_id, removed_item.owner_key
                )
        if session_key and revoked_event_id:
            self._discard_open_event(session_key, revoked_event_id)
            for registry in (
                self._active_state().session_next_run_event_ids,
                self._active_state().session_running_event_ids,
            ):
                ids = registry.get(session_key)
                if ids and revoked_event_id in ids:
                    ids.remove(revoked_event_id)
            await self._complete_event_if_needed(
                revoked_event_id, status=STATUS_CANCELED, message="revoked",
            )

        pending_event = self._pending_messages.get(session_key or "")
        if pending_event and pending_event.message_id == revoke.message_id:
            # 内部丢弃：绕过 pending pop 通知（这不是排队消息被轮次消费）。
            dict.pop(self._pending_messages, session_key, None)
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
        """处理后端下发的 event_cancel：精确取消单个事件（对齐 connector）。

        排队中 → 直接从队列摘除并以 canceled 收口；运行中 → 只停该事件所在
        的执行轮次，不影响同会话其他排队事件；都不是 → accepted=False。
        """
        client = self._active_client()
        if not client:
            return

        try:
            cancel = normalize_event_cancel(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid event_cancel payload: %s", self.name, exc)
            return

        try:
            if self._event_queue.cancel_queued(cancel.event_id):
                # canceled 终态与快照由 state_change 回调统一上报。
                await client.send_event_cancel_result(
                    event_id=cancel.event_id,
                    accepted=True,
                    final_state="canceled",
                )
                return

            item = self._event_queue.find(cancel.event_id)
            if item is not None and self._event_queue.is_running(cancel.event_id):
                _message, source, session_key = item.payload
                await self._force_stop_session(
                    source,
                    session_key,
                    reply_to=cancel.event_id,
                )
                # 正常由轮次的 CANCELLED 收口钩子上报终态；这里兜底一次
                # （幂等），防止轮次尚未真正启动时取消信号丢失。
                await self._complete_event_if_needed(
                    cancel.event_id,
                    status=STATUS_CANCELED,
                    message="event canceled by user",
                )
                await client.send_event_cancel_result(
                    event_id=cancel.event_id,
                    accepted=True,
                    final_state="canceled",
                )
                await self._push_queue_snapshot(item.session_id, item.owner_key)
                return

            # 队列不认识的事件（斜杠命令绕行直投等）：回退旧语义——停掉该
            # 会话正在跑的轮次并以终态收口，保证这类事件同样可被取消。
            stop_keys = self._active_session_keys_for_session(cancel.session_id)
            if stop_keys:
                source = self._active_state().latest_sources.get(cancel.session_id)
                if source is None:
                    source = self.build_source(chat_id=cancel.session_id, chat_type="dm")
                for key in stop_keys:
                    await self._force_stop_session(source, key, reply_to=cancel.event_id)
                await self._complete_event_if_needed(
                    cancel.event_id,
                    status=STATUS_CANCELED,
                    message="event canceled by user",
                )
                await client.send_event_cancel_result(
                    event_id=cancel.event_id,
                    accepted=True,
                    final_state="canceled",
                )
                return

            await client.send_event_cancel_result(
                event_id=cancel.event_id,
                accepted=False,
                reason="event not found or not cancelable",
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
                await client.send_event_cancel_result(
                    event_id=cancel.event_id,
                    accepted=False,
                    reason=str(exc),
                )
            except Exception:
                pass

    async def _handle_queue_clear_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 queue_clear：清空某会话的排队事件并上报结果。

        对齐 connector：只清"排队中（尚未运行）"的事件（逐条以 canceled 收口），
        不打断正在运行的轮次——停止运行中事件走 event_stop / event_cancel。
        """
        client = self._active_client()
        if not client:
            return

        try:
            clear = normalize_queue_clear(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid queue_clear payload: %s", self.name, exc)
            return

        owner_key = self._active_owner_key()
        try:
            cleared = self._event_queue.clear(
                clear.session_id,
                owner_key,
                reason="canceled by queue clear",
            )
            # 每条被清事件的 canceled 终态由 state_change 回调统一上报。
            await client.send_queue_clear_result(
                session_id=clear.session_id,
                success=True,
                canceled_event_ids=[item.event_id for item in cleared],
            )
            await self._push_queue_snapshot(clear.session_id, owner_key)
        except Exception as exc:
            logger.error(
                "[%s] queue_clear handler failed for %s: %s",
                self.name,
                clear.session_id,
                exc,
                exc_info=True,
            )
            try:
                await client.send_queue_clear_result(
                    session_id=clear.session_id,
                    success=False,
                    message=str(exc),
                )
            except Exception:
                pass

    async def _handle_queue_reorder_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 queue_reorder：按期望顺序重排某会话的排队事件。

        愿望清单语义（对齐 connector）：清单里已出队的 id 忽略，队列里新入队
        的项按原相对顺序排在尾部，绝不报错；回包带应用后的实际顺序。
        """
        client = self._active_client()
        if not client:
            return

        try:
            reorder = normalize_queue_reorder(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid queue_reorder payload: %s", self.name, exc)
            return

        owner_key = self._active_owner_key()
        try:
            applied = self._event_queue.reorder(
                reorder.session_id,
                list(reorder.ordered_event_ids),
                owner_key,
            )
            await client.send_queue_reorder_result(
                session_id=reorder.session_id,
                applied_event_ids=applied,
            )
            await self._push_queue_snapshot(reorder.session_id, owner_key)
        except Exception as exc:
            logger.error(
                "[%s] queue_reorder handler failed for %s: %s",
                self.name,
                reorder.session_id,
                exc,
                exc_info=True,
            )

    def _find_owned_queued_item(
        self, event_id: str, session_id: str, owner_key: str
    ) -> Optional[QueueItem]:
        """按 (event_id, session_id, owner_key) 精确命中一个「排队中」的事件。

        运行中/不存在/会话不符/owner 不符（共享场景防串 owner）都视为未命中，
        协议层统一回 not_found。
        """
        item = self._event_queue.find(event_id)
        if item is None or self._event_queue.is_running(event_id):
            return None
        if item.session_id != session_id or item.owner_key != owner_key:
            return None
        return item

    async def _handle_event_hold_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 event_hold：暂停/恢复单个排队事件（对齐 connector）。

        仅命中 queued[]；hold=True 施加持有（缺省永久阻塞，显式 ttl_ms 时重复
        调用重置 TTL），hold=False 解除。运行中/不存在 → ok=False error=not_found。
        """
        client = self._active_client()
        if not client:
            return

        try:
            hold_ev = normalize_event_hold(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid event_hold payload: %s", self.name, exc)
            try:
                await client.send_event_hold_result(
                    session_id=str(payload.get("session_id") or ""),
                    event_id=str(payload.get("event_id") or ""),
                    ok=False,
                    held=False,
                    error="bad_request",
                )
            except Exception:
                pass
            return

        owner_key = self._active_owner_key()
        try:
            item = self._find_owned_queued_item(
                hold_ev.event_id, hold_ev.session_id, owner_key
            )
            if item is None:
                await client.send_event_hold_result(
                    session_id=hold_ev.session_id,
                    event_id=hold_ev.event_id,
                    ok=False,
                    held=False,
                    error="not_found",
                )
                return

            if hold_ev.hold:
                self._event_queue.hold(
                    hold_ev.event_id,
                    reason=hold_ev.reason or "manual",
                    ttl_ms=hold_ev.ttl_ms,
                )
            else:
                self._event_queue.release(hold_ev.event_id)
            await client.send_event_hold_result(
                session_id=hold_ev.session_id,
                event_id=hold_ev.event_id,
                ok=True,
                held=item.held,
            )
            await self._push_queue_snapshot(hold_ev.session_id, owner_key)
        except Exception as exc:
            logger.error(
                "[%s] event_hold handler failed for %s: %s",
                self.name,
                hold_ev.event_id,
                exc,
                exc_info=True,
            )
            try:
                await client.send_event_hold_result(
                    session_id=hold_ev.session_id,
                    event_id=hold_ev.event_id,
                    ok=False,
                    held=False,
                    error="bad_request",
                )
            except Exception:
                pass

    async def _handle_queue_edit_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 queue_edit：改写单个排队事件的文本（对齐 connector）。

        仅命中 queued[]；成功后自动解除该事件的 hold，并把 QueueItem.payload
        里冻结的入站消息同步为新文本（否则轮到执行时用的还是旧文）。
        """
        client = self._active_client()
        if not client:
            return

        try:
            edit_ev = normalize_queue_edit(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid queue_edit payload: %s", self.name, exc)
            try:
                await client.send_queue_edit_result(
                    session_id=str(payload.get("session_id") or ""),
                    event_id=str(payload.get("event_id") or ""),
                    ok=False,
                    error="bad_request",
                )
            except Exception:
                pass
            return

        owner_key = self._active_owner_key()
        try:
            if not edit_ev.text.strip():
                await client.send_queue_edit_result(
                    session_id=edit_ev.session_id,
                    event_id=edit_ev.event_id,
                    ok=False,
                    error="empty_content",
                )
                return

            item = self._find_owned_queued_item(
                edit_ev.event_id, edit_ev.session_id, owner_key
            )
            if item is None:
                await client.send_queue_edit_result(
                    session_id=edit_ev.session_id,
                    event_id=edit_ev.event_id,
                    ok=False,
                    error="not_found",
                )
                return

            updated = self._event_queue.edit(edit_ev.event_id, edit_ev.text)
            if updated is None:
                # 竞态兜底：find 与 edit 之间事件出队（理论上单线程不会发生）。
                await client.send_queue_edit_result(
                    session_id=edit_ev.session_id,
                    event_id=edit_ev.event_id,
                    ok=False,
                    error="not_found",
                )
                return

            # 同步 payload 里冻结的入站消息（与 _handle_edit_packet 同款）：
            # 投递时用的是 payload[0].text，只改 content 不改 payload 会白编辑。
            q_payload = updated.payload if isinstance(updated.payload, tuple) else None
            q_message = q_payload[0] if q_payload else None
            if q_message is not None and dataclasses.is_dataclass(q_message):
                new_message = dataclasses.replace(q_message, text=edit_ev.text)
                updated.payload = (new_message, q_payload[1], q_payload[2])
            elif q_message is not None and hasattr(q_message, "text"):
                q_message.text = edit_ev.text

            await client.send_queue_edit_result(
                session_id=edit_ev.session_id,
                event_id=edit_ev.event_id,
                ok=True,
            )
            await self._push_queue_snapshot(edit_ev.session_id, owner_key)
        except Exception as exc:
            logger.error(
                "[%s] queue_edit handler failed for %s: %s",
                self.name,
                edit_ev.event_id,
                exc,
                exc_info=True,
            )
            try:
                await client.send_queue_edit_result(
                    session_id=edit_ev.session_id,
                    event_id=edit_ev.event_id,
                    ok=False,
                    error="bad_request",
                )
            except Exception:
                pass

    async def _handle_queue_snapshot_query_packet(self, payload: Dict[str, Any]) -> None:
        """处理后端下发的 queue_snapshot_query：立即回一条该会话的队列快照。

        会话没有任何排队/运行事件时回空快照——前端靠它清理本地残留状态
        （push 通道丢消息时的兜底通道，对齐 connector）。
        """
        if not self._active_client():
            return

        try:
            query = normalize_queue_snapshot_query(payload)
        except ValueError as exc:
            logger.warning("[%s] invalid queue_snapshot_query payload: %s", self.name, exc)
            return

        await self._push_queue_snapshot(query.session_id, self._active_owner_key())

    # ── 事件队列接线 ────────────────────────────────────────────────────

    def _client_for_owner(self, owner_key: str) -> Optional[GrixTransportClient]:
        """按 owner_key 解析当前存活连接：主连接或对应被共享者的子连接。"""
        if owner_key and owner_key != _PRIMARY_OWNER_KEY:
            return self._shared_clients.get(owner_key)
        return self._client

    def _track_background_task(self, task: "asyncio.Task") -> None:
        """把任务挂进框架的 _background_tasks 强引用集合，防止被 GC 半途回收。"""
        tasks = getattr(self, "_background_tasks", None)
        if tasks is None:
            return
        try:
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        except TypeError:
            pass

    def _on_queue_deliver(self, item: QueueItem) -> None:
        """队列回调：事件获得执行槽位。

        回调本身须同步返回。默认 fire-and-forget 派后台任务；若正处于
        ``_complete_event_if_needed`` 的同步汇聚窗口（``_sync_deliver_bucket``
        非空），则把协程挂进桶里由收口方 await——保证续投在当前轮
        ``on_processing_complete`` 返回前完成，从而能被 base 的 late_pending
        检查捞到，避免 pending 孤儿。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error(
                "[%s] queue deliver without running loop event_id=%s", self.name, item.event_id,
            )
            return
        coro = self._deliver_queued_event(item)
        bucket = getattr(self, "_sync_deliver_bucket", None)
        if bucket is not None:
            bucket.append(coro)
            return
        self._track_background_task(loop.create_task(coro))

    async def _deliver_queued_event(self, item: QueueItem) -> None:
        """投递一条队列事件：还原来源连接上下文后走正常派发链路。

        排队事件的投递发生在原 packet 上下文之外（上一轮收口触发的续投），
        必须显式把 _CURRENT_CLIENT_CTX 还原为事件来源 owner 的存活连接，
        保证回执/状态都从同一条连接发出。
        """
        message, source, session_key = item.payload
        # 竞态守卫：投递任务被调度后、真正运行前，事件可能已被取消/收口
        # （event_cancel / 撤回抢在投递前到达，槽位已释放）。此时放弃投递，
        # 避免把一条已上报 canceled 的消息交给 agent 执行。
        if not self._event_queue.is_running(item.event_id):
            logger.info(
                "[%s] queued event %s no longer running at delivery time, skipping",
                self.name, item.event_id,
            )
            return
        client = self._client_for_owner(item.owner_key)
        if client is None:
            # 事件来源 owner 的连接已不在（共享撤销/子连接关闭）：无法以正确
            # 身份投递与回执。释放槽位丢弃，绝不回落主连接串 owner 状态。
            logger.warning(
                "[%s] dropping queued event %s: owner connection gone owner_key=%s",
                self.name, item.event_id, item.owner_key,
            )
            self._event_queue.complete(item.event_id)
            return
        token = _CURRENT_CLIENT_CTX.set(client)
        try:
            await self._dispatch_grix_event(message, source, session_key)
        except Exception as exc:
            logger.error(
                "[%s] queued event dispatch failed event_id=%s: %s",
                self.name,
                item.event_id,
                exc,
                exc_info=True,
            )
            # _dispatch_grix_event 的异常路径已按 failed 收口（并释放槽位）；
            # 这里只兜异常本身，避免炸掉投递任务。
        finally:
            _CURRENT_CLIENT_CTX.reset(token)

    def _schedule_orphaned_pending_recovery(self, session_key: str, event_id: str) -> None:
        """busy-pending 进队后挂短恢复：会话空闲仍未认领则强制 drain。

        主路径靠 ``_complete_event_if_needed`` 同步 await 续投，让 base
        late_pending 捞到；本恢复是兜底（例如续投不经 complete 汇聚桶、
        或 late_pending 竞态仍漏掉）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._track_background_task(
            loop.create_task(self._recover_orphaned_pending(session_key, event_id))
        )

    async def _recover_orphaned_pending(self, session_key: str, event_id: str) -> None:
        # 先让出给当前轮的 late_pending / finally 清理。
        await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        if not self._event_still_open(session_key, event_id):
            return

        # 会话仍在跑且 owner task 未死：交给正常 drain。
        if session_key in self._active_sessions:
            stale = False
            try:
                stale = bool(self._session_task_is_stale(session_key))
            except Exception:
                stale = False
            if not stale:
                # 再等一会儿；长任务结束后应走 late_pending。若仍空闲悬挂则恢复。
                for _ in range(40):  # ~2s
                    await asyncio.sleep(0.05)
                    if not self._event_still_open(session_key, event_id):
                        return
                    if session_key not in self._active_sessions:
                        break
                    try:
                        if self._session_task_is_stale(session_key):
                            break
                    except Exception:
                        break
                else:
                    return

        if not self._event_still_open(session_key, event_id):
            return
        if session_key in self._active_sessions:
            try:
                if not self._session_task_is_stale(session_key):
                    return
                # 僵死锁：清掉再强制开跑（heal 会丢 pending，先取出）。
                pending = self._pending_messages.get(session_key)
                self._heal_stale_session_lock(session_key)
                if pending is not None and session_key not in self._pending_messages:
                    self._pending_messages[session_key] = pending
            except Exception:
                logger.debug(
                    "[%s] orphaned-pending stale-heal failed session=%s event=%s",
                    self.name, session_key, event_id, exc_info=True,
                )
                return

        pending = self._pending_messages.get(session_key)
        if pending is None:
            queue = getattr(self, "_event_queue", None)
            if queue is not None and queue.is_running(event_id):
                logger.warning(
                    "[%s] releasing ghost EventQueue running slot event=%s "
                    "(open but no pending/active session)",
                    self.name, event_id,
                )
                self._discard_open_event(session_key, event_id)
                await self._complete_event_if_needed(
                    event_id,
                    status=STATUS_FAILED,
                    message="orphaned running event",
                )
            return

        logger.warning(
            "[%s] recovering orphaned pending event=%s session=%s",
            self.name, event_id, session_key,
        )
        event = self.get_pending_message(session_key)
        if event is None:
            return
        if session_key in self._active_sessions:
            # 恢复窗口内又有新轮次：塞回 pending 让它 drain。
            self._pending_messages[session_key] = event
            return
        try:
            await self.handle_message(event)
        except Exception:
            logger.exception(
                "[%s] orphaned pending recovery failed event=%s", self.name, event_id,
            )
            if self._event_still_open(session_key, event_id):
                self._discard_open_event(session_key, event_id)
                await self._complete_event_if_needed(
                    event_id,
                    status=STATUS_FAILED,
                    message="orphaned pending recovery failed",
                )

    def _on_queue_state_change(self, item: QueueItem, state: str, meta: Dict[str, Any]) -> None:
        """队列回调：事件状态变化。派后台任务上报（回调本身须同步返回）。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._track_background_task(
            loop.create_task(self._report_queue_state(item, state, dict(meta)))
        )

    async def _report_queue_state(self, item: QueueItem, state: str, meta: Dict[str, Any]) -> None:
        """上报队列事件状态（event_state + 快照）；队列级终态补发 event_result。

        canceled/failed 表示事件从未进入执行（排队中被取消/清空/超时/队满
        拒绝），不会有轮次收口钩子替它发终态，必须在这里补发 event_result
        （对齐 connector：queued 态终结需要发 event_result）。
        """
        client = self._client_for_owner(item.owner_key)
        token = _CURRENT_CLIENT_CTX.set(client) if client is not None else None
        try:
            if client is not None:
                extra: Dict[str, Any] = {
                    "content_preview": item.preview,
                    "content": item.content,
                    "held": item.held,
                    "held_reason": item.held_reason,
                }
                if state == QUEUE_STATE_QUEUED:
                    extra["queue_position"] = meta.get("queue_position")
                    extra["queue_total"] = meta.get("queue_total")
                    extra["actions"] = [{"type": "cancel"}]
                elif state == QUEUE_STATE_RUNNING:
                    extra["actions"] = [{"type": "stop"}]
                elif meta.get("reason"):
                    extra["reason"] = meta["reason"]
                try:
                    await client.send_event_state(
                        event_id=item.event_id,
                        session_id=item.session_id,
                        state=state,
                        extra=extra,
                    )
                except Exception as exc:
                    logger.debug("[%s] send_event_state failed for %s: %s", self.name, item.event_id, exc)
                await self._push_queue_snapshot(item.session_id, item.owner_key)

            if state in (QUEUE_STATE_CANCELED, QUEUE_STATE_FAILED):
                status = STATUS_CANCELED if state == QUEUE_STATE_CANCELED else STATUS_FAILED
                await self._complete_event_if_needed(
                    item.event_id,
                    status=status,
                    message=str(meta.get("reason") or ""),
                )
        finally:
            if token is not None:
                _CURRENT_CLIENT_CTX.reset(token)

    def _active_bg_hold_processes(self, session_key: str) -> List[Dict[str, Any]]:
        """返回该 session 下仍应保活本轮 running 的后台进程。"""
        key = str(session_key or "").strip()
        if not key:
            return []
        try:
            from tools.process_registry import process_registry
        except Exception as exc:
            logger.warning(
                "[%s] bg-hold unavailable (process_registry import failed): %s",
                self.name, exc,
            )
            return []
        try:
            sessions = process_registry.list_sessions(session_key=key)
        except Exception as exc:
            logger.warning(
                "[%s] bg-hold process lookup failed for %s: %s",
                self.name, key, exc,
            )
            return []

        max_age = float(getattr(self, "_BG_HOLD_MAX_AGE_S", 0) or 0)
        hold: List[Dict[str, Any]] = []
        for proc in sessions:
            if not isinstance(proc, dict) or proc.get("status") != "running":
                continue
            if max_age > 0:
                try:
                    uptime = float(proc.get("uptime_seconds", 0) or 0)
                except (TypeError, ValueError):
                    uptime = 0.0
                if uptime >= max_age:
                    continue
            # Plain long-lived daemons have no terminal signal that will close
            # the user's turn. They remain tracked by process_registry, but
            # must not keep Grix composing/running alive.
            if not proc.get("notify_on_complete") and not proc.get("watch_patterns"):
                continue
            hold.append(proc)
        return hold

    def _session_has_bg_hold(self, session_key: str) -> bool:
        """该 Hermes session_key 是否仍有未退出的后台进程需要保活队列。"""
        return bool(self._active_bg_hold_processes(session_key))

    def _bg_hold_label(self, session_key: str) -> str:
        """取该会话最新活跃后台进程的命令预览，作虚拟 running 标题。"""
        key = str(session_key or "").strip()
        if not key:
            return ""
        running = self._active_bg_hold_processes(key)
        if not running:
            return ""
        # 最新启动的优先（started_at 为可读字符串时退回命令序）。
        chosen = running[-1]
        command = str(chosen.get("command") or "").strip()
        return build_preview(command) if command else ""

    async def _refresh_bg_hold_activity(
        self, session_id: str, owner_key: str, *, active: bool
    ) -> None:
        """续期/关闭 composing，让前端在虚拟 running 期间仍显示忙碌。"""
        client = self._client_for_owner(owner_key)
        if client is None:
            return
        try:
            await client.set_session_activity(
                session_id=str(session_id),
                kind="composing",
                active=bool(active),
                ttl_ms=self._BG_HOLD_COMPOSING_TTL_MS if active else None,
            )
        except Exception as exc:
            logger.debug(
                "[%s] bg-hold session_activity failed session=%s active=%s: %s",
                self.name, session_id, active, exc,
            )

    def _ensure_provider_quota_refresh(self) -> None:
        """确保厂商配额巡检任务在跑（幂等，对齐 connector startProviderQuotaTimer）。"""
        if getattr(self, "_shutting_down", False) or getattr(self, "_disconnect_requested", False):
            return
        task = getattr(self, "_provider_quota_task", None)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._provider_quota_task = loop.create_task(self._provider_quota_refresh_loop())

    async def _provider_quota_refresh_loop(self) -> None:
        """30s 巡检：查询当前生效 provider 配额，成功后补推工具栏快照。"""
        interval = float(getattr(self, "_PROVIDER_QUOTA_REFRESH_INTERVAL_S", 30.0) or 30.0)
        try:
            while not getattr(self, "_shutting_down", False):
                try:
                    refreshed = await self._refresh_provider_quota_once()
                    if refreshed:
                        await self._push_all_queue_snapshots()
                except Exception as exc:
                    # 单次巡检失败不退出循环（对齐 bg-hold sweep 的容错策略）。
                    logger.debug("[%s] provider-quota refresh error: %s", self.name, exc)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _refresh_provider_quota_once(self) -> bool:
        """查询一次配额并更新缓存；成功（拿到可用配额）返回 True。"""
        source = resolve_provider_quota_source()
        if source is None:
            return False
        try:
            snapshot = await shared_provider_quota_service.query(source)
        except Exception as exc:
            logger.debug("[%s] provider-quota query failed: %s", self.name, exc)
            return False
        quota = snapshot["quota"]
        if not quota.get("success"):
            logger.debug(
                "[%s] provider-quota query unsuccessful: provider=%s error=%s",
                self.name,
                quota.get("provider"),
                quota.get("error"),
            )
            return False
        self._provider_quota = quota
        self._provider_quota_sampled_at_ms = int(snapshot["sampledAt"])
        logger.info(
            "[%s] provider-quota refreshed: provider=%s tiers=%s%s",
            self.name,
            quota.get("provider"),
            ",".join(
                f"{t.get('name')}={t.get('usedPercent')}%" for t in quota.get("tiers") or []
            ),
            (
                f" balance={quota['balance'].get('remaining')} {quota['balance'].get('unit')}"
                if quota.get("balance")
                else ""
            ),
        )
        return True

    def _provider_quota_toolbar_meta(self) -> Dict[str, Any]:
        """把缓存的配额转成工具栏 meta 字段（provider_quota + rate_limits）。

        测试用 __new__ 构造的裸 adapter 没有 _provider_quota，getattr 兜底。
        """
        quota = getattr(self, "_provider_quota", None)
        if not quota or not quota.get("success"):
            return {}
        meta: Dict[str, Any] = {"provider_quota": quota}
        sampled_at = int(getattr(self, "_provider_quota_sampled_at_ms", 0) or 0)
        rate_limits = provider_quota_to_rate_limits(quota, sampled_at)
        if rate_limits:
            meta["rate_limits"] = rate_limits
        return meta

    def _ensure_bg_hold_sweep(self) -> None:
        """确保后台保活巡检任务在跑（幂等）。"""
        if getattr(self, "_shutting_down", False) or getattr(self, "_disconnect_requested", False):
            return
        task = getattr(self, "_bg_hold_sweep_task", None)
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._bg_hold_sweep_task = loop.create_task(self._bg_hold_sweep_loop())

    async def _bg_hold_sweep_loop(self) -> None:
        """巡检 bg_hold 会话：进程仍在则续 composing；已退出则清虚拟 running。"""
        interval = float(getattr(self, "_BG_HOLD_SWEEP_INTERVAL_S", 15.0) or 15.0)
        try:
            while not getattr(self, "_shutting_down", False):
                await asyncio.sleep(interval)
                try:
                    await self._sweep_bg_holds_once()
                except Exception as exc:
                    # 单次巡检失败不退出循环，避免僵尸虚拟 running 无人收。
                    logger.debug("[%s] bg-hold sweep once error: %s", self.name, exc)
                if not self._has_any_bg_hold():
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[%s] bg-hold sweep loop error: %s", self.name, exc)
        finally:
            if getattr(self, "_bg_hold_sweep_task", None) is asyncio.current_task():
                self._bg_hold_sweep_task = None

    def _has_any_bg_hold(self) -> bool:
        for state in self._owner_states.values():
            for work in state.toolbar_active_work.values():
                if work.get("bg_hold"):
                    return True
        return False

    async def _sweep_bg_holds_once(self) -> None:
        for owner_key, state in list(self._owner_states.items()):
            for session_key, work in list(state.toolbar_active_work.items()):
                if not work.get("bg_hold"):
                    continue
                session_id = str(work.get("session_id") or "").strip()
                if not session_id:
                    state.toolbar_active_work.pop(session_key, None)
                    continue
                # 真实轮次已接管时，去掉 bg_hold 标记，避免与 turn 标题打架。
                if state.session_running_event_ids.get(session_key):
                    work.pop("bg_hold", None)
                    continue
                if self._session_has_bg_hold(session_key):
                    title = self._bg_hold_label(session_key)
                    if title and title != work.get("title"):
                        work["title"] = title
                        await self._push_queue_snapshot(session_id, owner_key)
                    await self._refresh_bg_hold_activity(
                        session_id, owner_key, active=True
                    )
                    continue
                state.toolbar_active_work.pop(session_key, None)
                await self._push_queue_snapshot(session_id, owner_key)
                await self._refresh_bg_hold_activity(
                    session_id, owner_key, active=False
                )
                logger.info(
                    "[%s] Cleared bg-hold virtual running for session %s",
                    self.name, session_id,
                )

    async def _push_all_queue_snapshots(self) -> None:
        """补推全部持有事件会话的队列快照（重连成功后对齐 connector onReconnected）。

        测试用 __new__ 构造的裸 adapter 没有 _event_queue，getattr 兜底。
        """
        queue = getattr(self, "_event_queue", None)
        refs = set(queue.session_refs()) if queue is not None else set()
        for owner_key, state in self._owner_states.items():
            refs.update(
                (work["session_id"], owner_key)
                for work in state.toolbar_active_work.values()
                if work.get("session_id")
            )
        for session_id, owner_key in sorted(refs):
            await self._push_queue_snapshot(session_id, owner_key)

    async def _push_queue_snapshot(self, session_id: str, owner_key: str) -> None:
        """向后端推送某会话的队列快照（空快照也推，供前端清理残留状态）。"""
        client = self._client_for_owner(owner_key)
        if client is None:
            return
        queue = getattr(self, "_event_queue", None)
        snap = (
            queue.snapshot(session_id, owner_key)
            if queue is not None
            else {"running": [], "running_items": [], "queued": []}
        )
        running_items = [
            {
                "event_id": entry["event_id"],
                "content_preview": entry["content_preview"],
                "title": entry["content_preview"],
                "summary": entry["content_preview"],
                "actions": [{"type": "stop"}],
            }
            for entry in snap["running_items"]
        ]
        running = list(snap["running"])
        if not running:
            state = self._state_for(owner_key)
            active_work = next(
                (
                    work
                    for work in state.toolbar_active_work.values()
                    if work.get("session_id") == session_id
                ),
                None,
            )
            if active_work is not None:
                virtual_id = f"selfdrive_{session_id}"
                title = active_work.get("title") or "Background task in progress"
                running.append(virtual_id)
                running_items.append(
                    {
                        "event_id": virtual_id,
                        "content_preview": title,
                        "title": title,
                        "summary": title,
                        "actions": [],
                    }
                )
        queued = [
            {
                "event_id": entry["event_id"],
                "position": entry["position"],
                "content_preview": entry["content_preview"],
                "content": entry["content"],
                "held": entry["held"],
                "held_reason": entry["held_reason"],
                "title": entry["content_preview"],
                "summary": entry["content_preview"],
                "actions": [{"type": "cancel"}],
            }
            for entry in snap["queued"]
        ]
        try:
            await client.send_queue_snapshot(
                session_id=session_id,
                running=running,
                running_items=running_items,
                queued=queued,
            )
        except Exception as exc:
            logger.debug("[%s] send_queue_snapshot failed for %s: %s", self.name, session_id, exc)

        model_id = getattr(self, "_toolbar_model_id", "")
        if model_id:
            try:
                await client.send_update_binding_card(
                    session_id=session_id,
                    worker_status="busy" if running else "ready",
                    meta={
                        "model_id": model_id,
                        "available_models": [
                            {"id": model_id, "displayName": model_id}
                        ],
                        **self._provider_quota_toolbar_meta(),
                    },
                )
            except Exception as exc:
                logger.debug(
                    "[%s] send toolbar model metadata failed for %s: %s",
                    self.name,
                    session_id,
                    exc,
                )

    async def _handle_stop_packet(self, payload: Dict[str, Any]) -> None:
        stop = normalize_stop_event(payload)

        # 停的是"排队中（尚未运行）"的事件：精确摘除即可，不打断正在运行的
        # 轮次、不动队列里其他事件（对齐 connector）。静默移除——不发 canceled
        # 终态，只回 stop 协议（ack + stopped）。
        removed = self._event_queue.remove_queued(stop.event_id)
        if removed is not None:
            logger.info(
                "[%s] GRIX event_stop removed queued(not-running) event event_id=%s stop_id=%s session_id=%s",
                self.name, stop.event_id, stop.stop_id, stop.session_id,
            )
            if self._client:
                await self._active_client().acknowledge_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    accepted=True,
                )
                await self._complete_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    status=STATUS_STOPPED,
                )
            await self._push_queue_snapshot(removed.session_id, removed.owner_key)
            return

        # 停运行中的事件：优先用队列里该事件的权威 payload 反推 source /
        # session_key——latest_sources[session_id] 是"最后发言者"，群会话按
        # 发起人分路时会拼出别人的 session_key 而停错目标。
        running_item = (
            self._event_queue.find(stop.event_id)
            if self._event_queue.is_running(stop.event_id)
            else None
        )
        if running_item is not None:
            _r_message, source, session_key = running_item.payload
        else:
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

        # 停止指令的幂等只看"该事件的停止是否已完成过"（completed_stop_results）。
        # 不能复用消息投递的 seen_event_ids 去重：stop 携带的 event_id 就是被停
        # 事件的 id，投递时必然已记录，会把首次停止误判为重复而漏发 stop_result
        # （connector 参考实现对 stop 不做事件级去重，每次都回终态）。
        prior_stop = self._active_state().completed_stop_results.get(stop.event_id)
        if prior_stop is not None:
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
        notify: bool = True,
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

        # 停止即终态：被丢弃的排队事件（discard_pending 消费 pending 时移交到
        # next_run）与残留未归属事件统一以 canceled 收口，避免后端悬挂。
        # 运行中轮次的事件由其 complete 钩子（CANCELLED 结局）自行收口；
        # 仍在派发途中（inflight）的事件不动，交给它自己的派发链路。
        state = self._active_state()
        inflight = self._inflight_dispatch_event_ids.get(session_key) or ()
        for registry in (state.session_next_run_event_ids, state.session_open_event_ids):
            ids = registry.pop(session_key, [])
            kept = [eid for eid in ids if eid in inflight]
            if kept:
                registry[session_key] = kept
            for eid in ids:
                if eid not in inflight:
                    await self._complete_event_if_needed(
                        eid, status=STATUS_CANCELED, message="stopped by user",
                    )

        try:
            await self.stop_typing(source.chat_id)
        except Exception:
            pass

        if notify:
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

    @staticmethod
    def _session_key_belongs_to_session(session_key: str, session_id: str) -> bool:
        """session_key 是否属于某个 Grix 会话。

        hermes 的 session_key 形如
        ``<ns>:grix:<chat_type>:<session_id>[:<thread_id>][:<user_id>]``，
        会话 id 是 uuid，与命名空间/平台/会话类型/用户 id 各段都不会撞。
        """
        if not session_id or not session_key:
            return False
        return session_id in session_key.split(":")

    def _active_session_keys_for_session(self, session_id: str) -> List[str]:
        """该 Grix 会话下所有正在跑的任务 key（群会话按发起人分路，可能不止一个）。"""
        return [
            key
            for key in list(self._active_sessions.keys())
            if self._session_key_belongs_to_session(key, session_id)
        ]

    def _session_has_unsettled_events(self, session_id: str) -> bool:
        """该会话是否还有未收口的事件（已登记、尚未发出 event_result）。"""
        state = self._active_state()
        for registry in (
            state.session_running_event_ids,
            state.session_next_run_event_ids,
            state.session_open_event_ids,
        ):
            for key, event_ids in registry.items():
                if event_ids and self._session_key_belongs_to_session(key, session_id):
                    return True
        return False

    def _session_has_queued_work(self, session_key: str) -> bool:
        """会话是否还有排队待处理的消息（pending 队列或文本防抖缓冲）。

        ``_text_debounce`` 是框架属性（base __init__ 创建），测试用 __new__
        构造的裸 adapter 没有它，用 getattr 兜底。
        """
        if session_key in self._pending_messages:
            return True
        return bool(getattr(self, "_text_debounce", {}).get(session_key))

    def _event_still_open(self, session_key: str, event_id: str) -> bool:
        return event_id in self._active_state().session_open_event_ids.get(session_key, [])

    def _discard_open_event(self, session_key: str, event_id: str) -> None:
        state = self._active_state()
        ids = state.session_open_event_ids.get(session_key)
        if not ids:
            return
        if event_id in ids:
            ids.remove(event_id)
        if not ids:
            state.session_open_event_ids.pop(session_key, None)

    def _on_pending_consumed(self, session_key: str, _event: Any) -> None:
        """pending 队列被消费（框架 drain / runner 注入 / 停止丢弃）时的归属移交。

        该会话所有已登记、不在派发途中的事件随队列一起移交：有正在运行的轮次
        就归它（runner 中途注入场景），否则归下一轮（轮末 drain 场景）。停止
        丢弃场景由 _force_stop_session 事后统一以 canceled 收口 next_run。
        """
        state = self._active_state()
        inflight = self._inflight_dispatch_event_ids.get(session_key) or ()
        open_ids = state.session_open_event_ids.get(session_key)
        if not open_ids:
            return
        moved = [eid for eid in open_ids if eid not in inflight]
        if not moved:
            return
        kept = [eid for eid in open_ids if eid in inflight]
        if kept:
            state.session_open_event_ids[session_key] = kept
        else:
            state.session_open_event_ids.pop(session_key, None)
        running = state.session_running_event_ids.get(session_key)
        target = running if running is not None else state.session_next_run_event_ids.setdefault(session_key, [])
        for eid in moved:
            if eid not in target:
                target.append(eid)

    async def _complete_event_if_needed(
        self,
        event_id: str,
        *,
        status: str,
        message: Optional[str] = None,
    ) -> None:
        # 事件收口是所有终态路径（responded/canceled/failed）的统一汇聚点：
        # 无论后续能否上报，先释放该事件占用的队列槽位并触发续投（幂等）。
        # 测试用 __new__ 构造的裸 adapter 没有 _event_queue，getattr 兜底。
        queue = getattr(self, "_event_queue", None)
        # 嵌套 complete（续投路径又失败收口）时复用外层桶，避免内层清空
        # 导致外层丢协程。
        own_bucket = getattr(self, "_sync_deliver_bucket", None) is None
        if queue is not None and event_id:
            if own_bucket:
                self._sync_deliver_bucket = []
            try:
                released = queue.find(event_id) if queue.is_running(event_id) else None
                queue.complete(event_id)
                if released is not None:
                    await self._push_queue_snapshot(released.session_id, released.owner_key)
                if own_bucket:
                    # complete() 用 call_soon 延迟 drain（对齐 connector
                    # queueMicrotask）。先 sleep(0) 冲掉该 tick，让续投进桶，
                    # 再 await 跑完——必须在 on_processing_complete 返回前
                    # 结束，否则 late_pending 捞不到，pending 孤儿 + EQ
                    # running 空挂到 30min run_timeout。
                    await asyncio.sleep(0)
                    while self._sync_deliver_bucket:
                        delivers = list(self._sync_deliver_bucket)
                        self._sync_deliver_bucket.clear()
                        results = await asyncio.gather(*delivers, return_exceptions=True)
                        for result in results:
                            if isinstance(result, Exception):
                                logger.error(
                                    "[%s] sync queue deliver failed during complete "
                                    "event=%s: %s",
                                    self.name,
                                    event_id,
                                    result,
                                    exc_info=result,
                                )
                        # 嵌套 complete 可能又 call_soon 了新的 drain
                        if self._sync_deliver_bucket is not None:
                            await asyncio.sleep(0)
            finally:
                if own_bucket:
                    self._sync_deliver_bucket = None

        if not self._client or not event_id or event_id in self._active_state().completed_event_ids:
            return
        # 无论本次发送成功与否都先记录终态：收口发生在任务结束时（距事件到达
        # 可能很久），连接此刻可能正好断开/重连中；先记账保证 dedup 生效，
        # 发送失败由重连后的 _replay_pending_completed_events 补发，不会永久丢失。
        self._active_state().completed_event_results[event_id] = {
            "status": status,
            "message": message,
        }
        self._active_state().completed_event_ids.add(event_id)
        try:
            # Prefer a ready client for immediate delivery, but still enqueue on
            # a disconnected client object so crash/reconnect can replay disk.
            client = await self._get_ready_client(operation="complete_event")
            persist_client = client or self._active_client() or self._client
            if persist_client is None:
                logger.warning(
                    "[%s] GRIX complete_event deferred (no client) for %s status=%s — "
                    "will replay after reconnect",
                    self.name,
                    event_id,
                    status,
                )
                return
            await persist_client.complete_event(
                event_id=event_id,
                status=status,
                message=message,
            )
        except Exception as exc:
            logger.warning(
                "[%s] GRIX complete_event send failed for %s status=%s — will replay "
                "after reconnect: %s",
                self.name,
                event_id,
                status,
                exc,
            )

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

        Durable terminals live in the transport outbox (disk). Memory
        ``completed_event_results`` remains a same-process dedup / replay aid;
        after reconnect, prefer the transport outbox replay first, then push
        any memory-only leftovers through ``complete_event`` (first-durable-wins).
        """
        client = self._active_client()
        if client is not None:
            with suppress(Exception):
                client.replay_terminal_outboxes()
        if not self._client or not self._active_state().completed_event_ids:
            return
        replayed = 0
        for eid in list(self._active_state().completed_event_ids):
            result = self._active_state().completed_event_results.get(eid)
            if not result:
                continue
            # Skip terminals already ACK'd / dead-lettered on disk so client
            # replacement cannot re-enqueue a settled verdict.
            settled_client = self._active_client() or self._client
            if settled_client is not None and getattr(
                settled_client, "is_terminal_settled", None
            ):
                try:
                    if settled_client.is_terminal_settled(eid):
                        continue
                except Exception:
                    pass
            try:
                await (self._active_client() or self._client).complete_event(
                    event_id=eid,
                    status=str(result.get("status") or STATUS_RESPONDED),
                    message=result.get("message"),
                )
                replayed += 1
            except Exception as exc:
                # 补发是断连容错的最后一道防线：失败必须可见，不能吞成 debug——
                # 静默失败会让事件在后端一直悬挂到超时，且无人察觉。
                logger.warning(
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
            # 缓存缺失（如清理淘汰）时兜底回终态，绝不静默吞掉 stop_result——
            # 服务端收不到 result 会让停止按钮永久 loading。
            await self._complete_stop(
                event_id=event_id,
                stop_id=stop_id,
                status=STATUS_ALREADY_FINISHED,
            )
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
