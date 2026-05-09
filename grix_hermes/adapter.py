"""Grix platform adapter for Hermes Agent — plugin version.

This adapter connects Hermes to the Grix/aibot platform via websocket,
registering as a plugin platform so it works without modifying hermes-agent core.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, ProcessingOutcome, SendResult
from gateway.session import build_session_key

from .compat import build_card_action_user_text, build_exec_approval_message
from .tool_progress_cards import (
    build_tool_execution_channel_data,
    detect_tool_progress,
)
from .contract import (
    CMD_EVENT_EDIT,
    CMD_EVENT_MSG,
    CMD_EVENT_REVOKE,
    CMD_EVENT_STOP,
    CMD_LOCAL_ACTION,
    ERR_APPROVAL_NOT_FOUND,
    ERR_INVALID_LOCAL_ACTION,
    ERR_MISSING_APPROVAL_ID,
    ERR_STOP_HANDLER_FAILED,
    ERR_UNSUPPORTED_DECISION,
    ERR_UNSUPPORTED_LOCAL_ACTION,
    LOCAL_ACTION_CREATE_FOLDER,
    LOCAL_ACTION_EXEC_APPROVE,
    LOCAL_ACTION_EXEC_REJECT,
    LOCAL_ACTION_FILE_LIST,
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
    GrixInboundMessage,
    GrixLocalAction,
    GrixRevokeEvent,
    GrixStopEvent,
    build_connection_config,
    normalize_edit_event,
    normalize_inbound_message,
    normalize_local_action,
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

_ROUTE_SESSION_KEY_PREFIX = "agent:main:grix:"
_EVENT_DEDUP_WINDOW_SECONDS = 300
_EVENT_DEDUP_MAX_SIZE = 1000


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


def _coerce_retryable(error: Exception) -> bool:
    if isinstance(error, (GrixConnectionClosedError, GrixDependencyError)):
        return True
    lowered = str(error).lower()
    if not lowered:
        return True
    if isinstance(error, GrixPacketError) and error.code == 4008:
        return True
    return any(
        token in lowered
        for token in (
            "connect",
            "connection",
            "network",
            "timeout",
            "temporarily unavailable",
            "closing transport",
            "not connected",
            "not authenticated",
            "broken pipe",
            "reset by peer",
            "too fast",
            "rate limit",
            "throttl",
        )
    )


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

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(_PLATFORM_VALUE))
        self.connection = build_grix_connection_config(config)
        self._client: Optional[GrixTransportClient] = None
        self._connector = None
        self._disconnect_requested = False
        self._token_lock_identity: Optional[str] = None
        self._completed_event_ids: set[str] = set()
        self._seen_event_ids: Dict[str, float] = {}
        self._completed_event_results: Dict[str, Dict[str, Optional[str]]] = {}
        self._completed_stop_results: Dict[str, Dict[str, Optional[str]]] = {}
        self._reply_event_ids: Dict[tuple[str, str], str] = {}
        self._latest_sources: Dict[str, Any] = {}
        self._message_sources: Dict[tuple[str, str], Any] = {}
        self._message_session_keys: Dict[tuple[str, str], str] = {}
        self._user_dm_session_ids: Dict[str, str] = {}
        self._user_dm_session_keys: Dict[str, str] = {}
        self._approval_state: Dict[str, Dict[str, Optional[str]]] = {}
        self._processing_message_ids: Dict[str, str] = {}
        self._revoked_message_keys: set[tuple[str, str]] = set()
        self._busy_ack_msg_ids: Dict[str, tuple[str, str]] = {}
        self._last_send_at: float = 0.0
        self._send_lock = asyncio.Lock()
        self._tool_progress_msg_ids: set[str] = set()

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
        client = self._client
        if not client:
            return None

        status = getattr(client, "status", None)
        if not isinstance(status, dict):
            return client

        connected = bool(status.get("connected"))
        authed = bool(status.get("authed"))
        if connected and (authed or not require_authed):
            return client

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
        client = self._client
        if not client:
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

    async def connect(self) -> bool:
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
            on_packet=self._handle_protocol_packet,
            on_status=self._handle_transport_status,
        )
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
        return True

    async def disconnect(self) -> None:
        self._disconnect_requested = True
        client = self._client
        self._client = None
        if client:
            try:
                await client.disconnect("adapter disconnect")
            except Exception as exc:
                logger.debug("[%s] GRIX disconnect failed: %s", self.name, exc)
        self._seen_event_ids.clear()
        self._completed_event_results.clear()
        self._completed_stop_results.clear()
        self._completed_event_ids.clear()
        self._tool_progress_msg_ids.clear()
        await self._safe_release_lock()
        self._mark_disconnected()

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        client = await self._get_ready_client(operation="send")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)

        # Detect tool progress and inject structured channel_data for card display.
        tp = detect_tool_progress(content)
        if tp:
            tool_name, preview = tp
            if metadata is None:
                metadata = {}
            else:
                metadata = dict(metadata)
            cd: Dict[str, Any] = dict(metadata.get("channel_data") or {})
            cd.update(build_tool_execution_channel_data(tool_name, preview))
            metadata["channel_data"] = cd

        await self._enforce_send_rate()

        source_hint = self._latest_sources.get(str(chat_id))
        session_id, thread_id = await resolve_grix_target(
            client,
            self.connection,
            str(chat_id),
            thread_id=self._metadata_thread_id(metadata),
            source_hint=source_hint,
        )
        event_id = None
        if reply_to:
            event_id = self._reply_event_ids.get((str(session_id), str(reply_to)))
        if not event_id and metadata:
            raw_event_id = metadata.get("event_id")
            if isinstance(raw_event_id, str) and raw_event_id.strip():
                event_id = raw_event_id.strip()
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
                    reply_to_message_id=reply_to if is_first else None,
                    thread_id=thread_id,
                    event_id=event_id if is_first else None,
                    biz_card=biz_card if is_first else None,
                    channel_data=channel_data if is_first else None,
                )
                if len(chunks) > 1 and index < len(chunks) - 1:
                    await asyncio.sleep(0.2)
            if event_id:
                await self._complete_event_if_needed(event_id, status=STATUS_RESPONDED)
            result = SendResult(
                success=bool(receipt and receipt.get("ok")),
                message_id=receipt.get("message_id") if receipt else None,
                raw_response=receipt,
                retryable=False,
            )
            # Track tool progress messages so edit_message can intercept them.
            if tp and result.success and result.message_id:
                self._tool_progress_msg_ids.add(result.message_id)
            if result.message_id and reply_to:
                _normalized_reply_to = str(reply_to).strip()
                for _sk, _pe in self._pending_messages.items():
                    if _pe and str(getattr(_pe, "message_id", "")).strip() == _normalized_reply_to:
                        self._busy_ack_msg_ids[_sk] = (str(chat_id), result.message_id)
                        logger.debug(
                            "[%s] Tracked busy-ack notification msg_id=%s for session_key=%s",
                            self.name, result.message_id, _sk,
                        )
                        break
            return result
        except Exception as exc:
            if event_id:
                await self._complete_event_if_needed(
                    event_id,
                    status=STATUS_FAILED,
                    message=str(exc),
                )
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

        source_hint = self._latest_sources.get(str(chat_id))
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
            self._approval_state[resolved_approval_id] = {
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
        if message_id in self._tool_progress_msg_ids:
            self._tool_progress_msg_ids.discard(message_id)
            return SendResult(success=False, error="tool_progress_card_fallback")

        _ = finalize
        client = await self._get_ready_client(operation="edit_message")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)
        try:
            source_hint = self._latest_sources.get(str(chat_id))
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
            return SendResult(
                success=False,
                error=str(exc),
                raw_response=exc,
                retryable=_coerce_retryable(exc),
            )

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
    ) -> SendResult:
        client = await self._get_ready_client(operation="delete_message")
        if not client:
            return SendResult(success=False, error="GRIX transport is not connected", retryable=True)
        try:
            source_hint = self._latest_sources.get(str(chat_id))
            session_id, _thread_id = await resolve_grix_target(
                client,
                self.connection,
                str(chat_id),
                source_hint=source_hint,
            )
            receipt = await client.delete_message(
                str(session_id),
                str(message_id),
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

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        client = await self._get_ready_client(operation="send_typing")
        if not client:
            return
        try:
            source_hint = self._latest_sources.get(str(chat_id))
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
        source = self._latest_sources.get(str(chat_id))
        if source:
            return {
                "id": source.chat_id,
                "name": source.chat_name or source.chat_id,
                "type": source.chat_type,
            }

        base_chat_id, _, thread_id = str(chat_id).partition(":")
        source = self._latest_sources.get(chat_id) or self._latest_sources.get(base_chat_id)
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
            ack_entry = self._busy_ack_msg_ids.pop(session_key, None)
            if ack_entry:
                chat_id, msg_id = ack_entry
                logger.debug(
                    "[%s] Scheduling busy-ack deletion msg_id=%s for session_key=%s",
                    self.name, msg_id, session_key,
                )
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._delete_busy_ack(chat_id, msg_id, session_key))
                except RuntimeError:
                    logger.warning(
                        "[%s] No running loop for busy-ack deletion msg_id=%s",
                        self.name, msg_id,
                    )
        return event

    async def _delete_busy_ack(self, chat_id: str, msg_id: str, session_key: str) -> None:
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
        if message_id and self._processing_message_ids.get(session_key) == message_id:
            self._processing_message_ids.pop(session_key, None)
        if message_id and self.is_message_revoked(session_key, message_id):
            self._revoked_message_keys.discard((session_key, message_id))
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
        self._processing_message_ids[session_key] = str(event.message_id)

    def is_message_revoked(self, session_key: str, message_id: str) -> bool:
        normalized_message_id = str(message_id or "").strip()
        if not session_key or not normalized_message_id:
            return False
        return (session_key, normalized_message_id) in self._revoked_message_keys

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

    async def _handle_transport_status(self, status: Dict[str, Any]) -> None:
        if self._disconnect_requested:
            return
        if status.get("connected", True):
            return
        if not self.is_connected:
            return

        message = str(status.get("last_error") or "grix websocket disconnected")
        self._set_fatal_error("grix_connection_lost", message, retryable=True)
        await self._notify_fatal_error()

    async def _handle_protocol_packet(self, packet: Dict[str, Any]) -> None:
        cmd = packet.get("cmd")
        payload = packet.get("payload") or {}
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
            else:
                logger.debug("[%s] Ignoring unknown GRIX packet %s", self.name, cmd)
        except Exception as exc:
            logger.error("[%s] Failed handling GRIX packet %s: %s", self.name, cmd, exc, exc_info=True)

    async def _handle_local_action_packet(self, payload: Dict[str, Any]) -> None:
        if not self._client:
            return

        action: GrixLocalAction = normalize_local_action(payload)
        if not action.action_id or not action.action_type:
            await self._client.send_local_action_result(
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

        if action.action_type not in {LOCAL_ACTION_EXEC_APPROVE, LOCAL_ACTION_EXEC_REJECT}:
            await self._client.send_local_action_result(
                action_id=action.action_id,
                status=STATUS_UNSUPPORTED,
                error_code=ERR_UNSUPPORTED_LOCAL_ACTION,
                error_message=f"unsupported local action: {action.action_type}",
            )
            return

        approval_id = _approval_lookup_id(action.params)
        if not approval_id:
            await self._client.send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_MISSING_APPROVAL_ID,
                error_message="approval_id is required",
            )
            return

        approval_choice, decision_value = _approval_choice_from_action(action.action_type, action.params)
        if approval_choice is None:
            await self._client.send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_UNSUPPORTED_DECISION,
                error_message=f"unsupported approval decision: {decision_value or action.action_type}",
            )
            return

        approval_state = self._approval_state.pop(approval_id, None)
        session_key = str((approval_state or {}).get("session_key") or "").strip()
        if not session_key:
            await self._client.send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_APPROVAL_NOT_FOUND,
                error_message="unknown or expired approval id",
            )
            return

        from tools.approval import resolve_gateway_approval

        resolved = resolve_gateway_approval(session_key, approval_choice)
        if resolved <= 0:
            await self._client.send_local_action_result(
                action_id=action.action_id,
                status=STATUS_FAILED,
                error_code=ERR_APPROVAL_NOT_FOUND,
                error_message="unknown or expired approval id",
            )
            return

        paused_chat_id = str((approval_state or {}).get("chat_id") or "").strip()
        if paused_chat_id:
            self.resume_typing_for_chat(paused_chat_id)

        await self._client.send_local_action_result(
            action_id=action.action_id,
            status=STATUS_OK,
            result=decision_value or approval_choice,
        )

    async def _handle_file_list(self, action: GrixLocalAction) -> None:
        from .file_list import handle_file_list_action, real_home_dir

        if not self._client:
            return
        result = handle_file_list_action(
            action.params,
            resolve_cwd=lambda _sid: None,
            fallback_dir=real_home_dir(),
        )
        await self._client.send_local_action_result(
            action_id=action.action_id,
            status=result["status"],
            result=result.get("result"),
            error_code=result.get("error_code"),
            error_message=result.get("error_msg"),
        )

    async def _handle_create_folder(self, action: GrixLocalAction) -> None:
        from .create_folder import handle_create_folder_action, real_home_dir

        if not self._client:
            return
        result = handle_create_folder_action(
            action.params,
            resolve_cwd=lambda _sid: None,
            fallback_dir=real_home_dir(),
        )
        await self._client.send_local_action_result(
            action_id=action.action_id,
            status=result["status"],
            result=result.get("result"),
            error_code=result.get("error_code"),
            error_message=result.get("error_msg"),
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
        prev_session_key = self._user_dm_session_keys.get(str(message.sender_id or "")) if message.chat_type == "dm" and message.sender_id else None
        prev_session_id = self._user_dm_session_ids.get(str(message.sender_id or "")) if message.chat_type == "dm" and message.sender_id else None
        self._latest_sources[message.session_id] = source
        self._latest_sources[session_key] = source
        if message.thread_id:
            self._latest_sources[f"{message.session_id}:{message.thread_id}"] = source
        self._reply_event_ids[(message.session_id, message.message_id)] = message.event_id
        self._message_sources[(message.session_id, message.message_id)] = source
        self._message_session_keys[(message.session_id, message.message_id)] = session_key

        if message.chat_type == "dm" and message.sender_id:
            sender_key = str(message.sender_id)
            self._user_dm_session_ids[sender_key] = str(message.session_id)
            self._user_dm_session_keys[sender_key] = session_key
            if prev_session_id and prev_session_id != str(message.session_id):
                logger.warning(
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
                await self._client.acknowledge_event(
                    event_id=message.event_id,
                    session_id=message.session_id,
                    message_id=message.message_id,
                )
                await self._replay_completed_event(message.event_id)
            logger.debug("[%s] Ignoring duplicate GRIX message event %s", self.name, message.event_id)
            return

        if self._client:
            await self._client.acknowledge_event(
                event_id=message.event_id,
                session_id=message.session_id,
                message_id=message.message_id,
            )
            self._schedule_session_route_bind(
                session_key=session_key,
                session_id=message.session_id,
            )

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
                self._processing_message_ids[session_key] = message.message_id
            await self.handle_message(event)
        except Exception as exc:
            if self._client:
                await self._complete_event_if_needed(
                    message.event_id,
                    status=STATUS_FAILED,
                    message=str(exc),
                )
            raise

    async def _handle_edit_packet(self, payload: Dict[str, Any]) -> None:
        edit: GrixEditEvent = normalize_edit_event(payload)
        session_key = self._message_session_keys.get((edit.session_id, edit.message_id))
        if not session_key:
            source = self._latest_sources.get(edit.session_id)
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
        source = self._message_sources.get((revoke.session_id, revoke.message_id))
        session_key = self._message_session_keys.get((revoke.session_id, revoke.message_id))
        if not session_key and source is not None:
            session_key = build_session_key(
                source,
                group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
                thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            )

        if self._client:
            await self._client.acknowledge_event(
                event_id=revoke.event_id,
                session_id=revoke.session_id,
                message_id=revoke.message_id,
            )

        pending_event = self._pending_messages.get(session_key or "")
        if pending_event and pending_event.message_id == revoke.message_id:
            self._pending_messages.pop(session_key, None)
            ack_entry = self._busy_ack_msg_ids.pop(session_key, None) if session_key else None
            if ack_entry:
                ack_chat_id, ack_msg_id = ack_entry
                await self._delete_busy_ack(ack_chat_id or revoke.session_id, ack_msg_id, session_key or "")
            logger.debug(
                "[%s] Dropped pending Hermes event from GRIX revoke for %s/%s",
                self.name,
                revoke.session_id,
                revoke.message_id,
            )
        elif session_key and self._processing_message_ids.get(session_key) == revoke.message_id:
            self._revoked_message_keys.add((session_key, revoke.message_id))
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

        self._reply_event_ids.pop((revoke.session_id, revoke.message_id), None)
        self._message_sources.pop((revoke.session_id, revoke.message_id), None)
        self._message_session_keys.pop((revoke.session_id, revoke.message_id), None)

    async def _handle_stop_packet(self, payload: Dict[str, Any]) -> None:
        stop = normalize_stop_event(payload)
        source = self._resolve_stop_source(stop)
        session_key = build_session_key(
            source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )
        was_active = session_key in self._active_sessions

        is_duplicate = self._remember_event_id(stop.event_id)
        if is_duplicate:
            if self._client:
                await self._client.acknowledge_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    accepted=True,
                )
                await self._replay_completed_stop(stop.event_id, stop.stop_id)
            logger.debug("[%s] Ignoring duplicate GRIX stop event %s", self.name, stop.event_id)
            return

        if self._client:
            await self._client.acknowledge_stop(
                event_id=stop.event_id,
                stop_id=stop.stop_id,
                accepted=True,
            )

        event = MessageEvent(
            text="/stop",
            message_type=MessageType.COMMAND,
            source=source,
            raw_message={**stop.raw, "_grix_kind": "stop"},
            message_id=stop.trigger_message_id or stop.event_id,
        )

        try:
            await self.handle_message(event)
            if self._client:
                await self._complete_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    status=STATUS_STOPPED if was_active else STATUS_ALREADY_FINISHED,
                )
        except Exception as exc:
            if self._client:
                await self._complete_stop(
                    event_id=stop.event_id,
                    stop_id=stop.stop_id,
                    status=STATUS_FAILED,
                    code=ERR_STOP_HANDLER_FAILED,
                    message=str(exc),
                )
            raise

    def _resolve_stop_source(self, stop: GrixStopEvent):
        source = self._latest_sources.get(stop.session_id)
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
        if not self._client or not event_id or event_id in self._completed_event_ids:
            return
        try:
            await self._client.complete_event(
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
        self._completed_event_results[event_id] = {
            "status": status,
            "message": message,
        }
        self._completed_event_ids.add(event_id)

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
        await self._client.complete_stop(
            event_id=event_id,
            stop_id=stop_id,
            status=status,
            code=code,
            message=message,
        )
        self._completed_stop_results[event_id] = {
            "status": status,
            "stop_id": stop_id,
            "code": code,
            "message": message,
        }

    async def _replay_completed_event(self, event_id: str) -> None:
        if not self._client:
            return
        result = self._completed_event_results.get(event_id)
        if not result:
            return
        await self._client.complete_event(
            event_id=event_id,
            status=str(result.get("status") or STATUS_RESPONDED),
            message=result.get("message"),
        )

    async def _replay_completed_stop(self, event_id: str, stop_id: Optional[str]) -> None:
        if not self._client:
            return
        result = self._completed_stop_results.get(event_id)
        if not result:
            return
        await self._client.complete_stop(
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
        if len(self._seen_event_ids) > _EVENT_DEDUP_MAX_SIZE:
            cutoff = now - _EVENT_DEDUP_WINDOW_SECONDS
            self._seen_event_ids = {
                key: ts for key, ts in self._seen_event_ids.items() if ts > cutoff
            }
            self._completed_event_results = {
                key: value
                for key, value in self._completed_event_results.items()
                if key in self._seen_event_ids
            }
            self._completed_stop_results = {
                key: value
                for key, value in self._completed_stop_results.items()
                if key in self._seen_event_ids
            }
            self._completed_event_ids = {
                key for key in self._completed_event_ids if key in self._seen_event_ids
            }

        if normalized_event_id in self._seen_event_ids:
            return True

        self._seen_event_ids[normalized_event_id] = now
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
