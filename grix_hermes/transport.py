"""Async websocket transport for the Grix/aibot protocol."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from .contract import (
    CMD_AGENT_INVOKE,
    CMD_AGENT_INVOKE_RESULT,
    CMD_AUTH,
    CMD_AUTH_ACK,
    CMD_AUDIT_STATE,
    CMD_EDIT_MSG,
    CMD_ERROR,
    CMD_EVENT_ACK,
    CMD_EVENT_CANCEL_RESULT,
    CMD_EVENT_HOLD_RESULT,
    CMD_EVENT_RESULT,
    CMD_EVENT_STATE,
    CMD_EVENT_STOP_ACK,
    CMD_EVENT_STOP_RESULT,
    CMD_LOCAL_ACTION_RESULT,
    CMD_LOCAL_ACTION_ACK,
    CMD_RELAY_CREDENTIAL_REQUEST,
    CMD_RELAY_CREDENTIAL_RESULT,
    CMD_RELAY_STATE_SYNC_REQUEST,
    CMD_RELAY_STATE_SYNC_RESULT,
    CMD_RELAY_STATE_REPORT,
    CMD_PING,
    CMD_PONG,
    CMD_QUEUE_CLEAR_RESULT,
    CMD_QUEUE_EDIT_RESULT,
    CMD_QUEUE_REORDER_RESULT,
    CMD_QUEUE_SNAPSHOT,
    CMD_SEND_ACK,
    CMD_SEND_MSG,
    CMD_SEND_NACK,
    CMD_SESSION_ACTIVITY_SET,
    CMD_SESSION_ROUTE_BIND,
    CMD_SESSION_ROUTE_RESOLVE,
    CMD_UPDATE_BINDING_CARD,
)
from .protocol import (
    DEFAULT_REQUEST_TIMEOUT_MS,
    GrixConnectionConfig,
    build_auth_payload,
    build_packet,
    decode_packet,
    encode_packet,
    parse_code,
    parse_heartbeat_sec,
    parse_message,
)
from .terminal_delivery import TerminalDeliveryController

logger = logging.getLogger(__name__)

try:
    import aiohttp
    from aiohttp import ClientSession, ClientTimeout, WSMsgType

    AIOHTTP_AVAILABLE = True
except ImportError:
    aiohttp = None
    ClientSession = Any
    ClientTimeout = Any
    WSMsgType = Any
    AIOHTTP_AVAILABLE = False


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _maybe_await(result: Any) -> Any:
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


class GrixTransportError(RuntimeError):
    """Base transport failure."""


class GrixPacketError(GrixTransportError):
    """Request failed with an error packet."""

    def __init__(self, cmd: str, code: int, message: str):
        super().__init__(f"grix {cmd}: code={code} msg={message}")
        self.cmd = cmd
        self.code = code


class GrixAuthRejectedError(GrixTransportError):
    """Authentication was rejected by the server."""

    def __init__(self, code: int, message: str):
        super().__init__(f"grix auth failed: code={code} msg={message}")
        self.code = code


class GrixConnectionClosedError(GrixTransportError):
    """Socket closed unexpectedly."""


class GrixDependencyError(GrixTransportError):
    """Missing optional runtime dependency."""


class GrixSocket(Protocol):
    async def send_text(self, text: str) -> None: ...

    async def receive(self) -> Dict[str, Any]: ...

    async def close(self, reason: str = "") -> None: ...


Connector = Callable[[GrixConnectionConfig], Awaitable[GrixSocket]]
PacketHandler = Callable[[Dict[str, Any]], Awaitable[None] | None]
StatusHandler = Callable[[Dict[str, Any]], Awaitable[None] | None]


@dataclass(frozen=True)
class GrixAuthSession:
    heartbeat_sec: int
    protocol: Optional[str] = None
    supported_capabilities: tuple[str, ...] = ()
    ack_policy: Optional[Dict[str, Any]] = None
    # 字符串化 int64（aibot AuthAckPayload.OwnerID json:"owner_id,string,omitempty"），
    # 旧服务端可能不携带；SkillSyncManager 按它给技能同步分桶。
    owner_id: Optional[str] = None


@dataclass
class _PendingRequest:
    expected: set[str]
    future: asyncio.Future
    timeout_handle: asyncio.TimerHandle


class _AiohttpSocket:
    def __init__(self, session: ClientSession, ws):
        self._session = session
        self._ws = ws

    async def send_text(self, text: str) -> None:
        await self._ws.send_str(text)

    async def receive(self) -> Dict[str, Any]:
        message = await self._ws.receive()
        if message.type == WSMsgType.TEXT:
            return {"kind": "text", "text": message.data}
        if message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING):
            return {"kind": "closed", "reason": getattr(self._ws, "close_reason", "") or ""}
        if message.type == WSMsgType.ERROR:
            return {"kind": "error", "error": self._ws.exception()}
        if message.type == WSMsgType.BINARY:
            return {"kind": "text", "text": message.data.decode("utf-8", errors="replace")}
        return {"kind": "error", "error": RuntimeError(f"unexpected websocket frame: {message.type}")}

    async def close(self, reason: str = "") -> None:
        try:
            await self._ws.close(message=reason.encode("utf-8")[:120] if reason else b"")
        finally:
            await self._session.close()


async def default_connector(config: GrixConnectionConfig) -> GrixSocket:
    if not AIOHTTP_AVAILABLE:
        raise GrixDependencyError("aiohttp is unavailable in this runtime")

    timeout = ClientTimeout(total=max(config.connect_timeout_ms / 1000, 1))
    session = ClientSession(timeout=timeout)
    try:
        ws = await session.ws_connect(
            config.endpoint,
            receive_timeout=None,
            heartbeat=30,
            timeout=max(config.connect_timeout_ms / 1000, 1),
        )
    except Exception:
        await session.close()
        raise
    return _AiohttpSocket(session, ws)


class GrixTransportClient:
    def __init__(
        self,
        config: GrixConnectionConfig,
        *,
        connector: Optional[Connector] = None,
        on_packet: Optional[PacketHandler] = None,
        on_status: Optional[StatusHandler] = None,
    ):
        self._config = config
        self._connector = connector or default_connector
        self.on_packet = on_packet
        self.on_status = on_status
        self._socket: Optional[GrixSocket] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._packet_tasks: set[asyncio.Task] = set()
        self._pending: Dict[int, _PendingRequest] = {}
        self._seq = int(time.time() * 1000)
        self._auth_session: Optional[GrixAuthSession] = None
        self._disconnect_requested = False
        self._disconnect_lock = asyncio.Lock()
        self._status_tasks: set[asyncio.Task] = set()
        self._status = {
            "running": False,
            "connected": False,
            "authed": False,
            "last_error": None,
            "last_connect_at": None,
            "last_disconnect_at": None,
        }
        self._connection_generation = 0
        self._negotiated_capabilities: set[str] = set()
        self._ack_policy: Optional[Dict[str, Any]] = None
        self._send_lock = asyncio.Lock()
        self._terminal = TerminalDeliveryController(self)

    @property
    def status(self) -> Dict[str, Any]:
        return dict(self._status)

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    @property
    def negotiated_capabilities(self) -> set[str]:
        return set(self._negotiated_capabilities)

    @property
    def ack_policy(self) -> Optional[Dict[str, Any]]:
        return dict(self._ack_policy) if self._ack_policy else None

    @property
    def owner_id(self) -> Optional[str]:
        """auth_ack 携带的 owner_id（未连接或旧服务端未下发时为 None）。"""
        return self._auth_session.owner_id if self._auth_session else None

    @property
    def is_ready_for_outbound(self) -> bool:
        return bool(
            self._socket
            and self._status.get("connected")
            and self._status.get("authed")
        )

    def is_connection_current(self, generation: int) -> bool:
        return (
            self._socket is not None
            and self._connection_generation == generation
            and bool(self._status.get("connected"))
            and bool(self._status.get("authed"))
        )

    async def reconnect_after_outbound_failure(self, reason: str) -> None:
        """Soft-close the socket without cancelling the calling delivery task.

        Aligns with connector ``reconnectAfterOutboundWriteFailure``: bump
        generation, clear caps/timers, reject pending requests, close socket,
        mark disconnected — leave delivery task cancellation to normal
        disconnect / adapter reconnect paths.
        """
        async with self._disconnect_lock:
            if self._disconnect_requested and not self._socket:
                return
            self._connection_generation += 1
            self._negotiated_capabilities.clear()
            self._ack_policy = None
            self._terminal.on_soft_disconnect()
            self._reject_pending(GrixTransportError(reason or "grix transport disconnected"))

            # Cancel reader/heartbeat only — not packet/delivery tasks that may
            # be the caller of this method.
            current_task = asyncio.current_task()
            side_tasks = [
                task
                for task in (self._heartbeat_task, self._reader_task)
                if task and task is not current_task
            ]
            for task in side_tasks:
                task.cancel()
            if side_tasks:
                await asyncio.gather(*side_tasks, return_exceptions=True)

            socket = self._socket
            self._socket = None
            self._heartbeat_task = None
            self._reader_task = None
            self._auth_session = None
            if socket:
                with suppress(Exception):
                    await socket.close(reason)
            self._update_status(
                {
                    "running": False,
                    "connected": False,
                    "authed": False,
                    "last_disconnect_at": _now_ms(),
                    "last_error": reason or None,
                }
            )

    def is_terminal_settled(self, event_id: str) -> bool:
        return self._terminal.is_terminal_settled(event_id)

    async def connect(self) -> GrixAuthSession:
        if self._status["connected"] and self._auth_session:
            return self._auth_session

        self._disconnect_requested = False
        self._connection_generation += 1
        self._update_status({"running": True, "last_error": None})
        self._socket = await self._connector(self._config)
        self._update_status(
            {
                "connected": True,
                "last_connect_at": _now_ms(),
                "last_error": None,
            }
        )
        self._reader_task = asyncio.create_task(self._reader_loop())

        try:
            auth_session = await self.authenticate()
        except Exception:
            await self.disconnect("auth failed")
            raise

        self._auth_session = auth_session
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(auth_session.heartbeat_sec)
        )
        self._terminal.on_authenticated()
        return auth_session

    async def disconnect(self, reason: str = "") -> None:
        async with self._disconnect_lock:
            self._disconnect_requested = True
            self._connection_generation += 1
            self._terminal.on_disconnect()
            self._negotiated_capabilities.clear()
            self._ack_policy = None
            current_task = asyncio.current_task()

            tasks = [
                task
                for task in (self._heartbeat_task, self._reader_task, *self._packet_tasks)
                if task and task is not current_task
            ]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            self._reject_pending(GrixTransportError(reason or "grix transport disconnected"))

            socket = self._socket
            self._socket = None
            self._heartbeat_task = None
            self._reader_task = None
            self._packet_tasks.clear()
            if socket:
                with suppress(Exception):
                    await socket.close(reason)

            self._auth_session = None
            self._update_status(
                {
                    "running": False,
                    "connected": False,
                    "authed": False,
                    "last_disconnect_at": _now_ms(),
                    "last_error": reason or None,
                }
            )

    async def authenticate(self) -> GrixAuthSession:
        packet = await self.request(
            CMD_AUTH,
            build_auth_payload(self._config),
            expected=(CMD_AUTH_ACK,),
            timeout_ms=10_000,
            require_authed=False,
        )
        code = parse_code(packet["payload"])
        if code != 0:
            raise GrixAuthRejectedError(code, parse_message(packet["payload"]))

        payload = packet["payload"] or {}
        raw_caps = payload.get("supported_capabilities")
        negotiated: set[str] = set()
        if isinstance(raw_caps, list):
            negotiated = {str(item).strip() for item in raw_caps if str(item).strip()}
        self._negotiated_capabilities = negotiated

        ack_policy = payload.get("ack_policy")
        self._ack_policy = dict(ack_policy) if isinstance(ack_policy, dict) else None
        if self._ack_policy:
            logger.info(
                "ack_policy received: push_ack_timeout_ms=%s max_retries=%s timeout_action=%s",
                self._ack_policy.get("push_ack_timeout_ms", "default"),
                self._ack_policy.get("max_retries", "default"),
                self._ack_policy.get("timeout_action", "default"),
            )

        raw_owner_id = payload.get("owner_id")
        owner_id = str(raw_owner_id).strip() if raw_owner_id is not None else ""

        auth_session = GrixAuthSession(
            heartbeat_sec=parse_heartbeat_sec(payload),
            protocol=(str(payload.get("protocol") or "").strip() or None),
            supported_capabilities=tuple(sorted(negotiated)),
            ack_policy=self._ack_policy,
            owner_id=owner_id or None,
        )
        self._update_status({"authed": True, "last_error": None})
        return auth_session

    async def send_packet(
        self,
        cmd: str,
        payload: Dict[str, Any],
        *,
        seq: Optional[int] = None,
        require_authed: bool = True,
    ) -> int:
        return await self._send_packet_internal(
            cmd,
            payload,
            seq=seq,
            require_authed=require_authed,
        )

    async def request(
        self,
        cmd: str,
        payload: Dict[str, Any],
        *,
        expected: tuple[str, ...] | list[str],
        timeout_ms: Optional[int] = None,
        require_authed: bool = True,
    ) -> Dict[str, Any]:
        self._ensure_ready(require_authed=require_authed)
        seq = self._next_seq()
        loop = asyncio.get_running_loop()
        future = loop.create_future()

        def _on_timeout() -> None:
            pending = self._pending.pop(seq, None)
            if pending and not pending.future.done():
                pending.future.set_exception(TimeoutError(f"{cmd} timeout"))

        handle = loop.call_later(
            (timeout_ms or self._config.request_timeout_ms or DEFAULT_REQUEST_TIMEOUT_MS) / 1000,
            _on_timeout,
        )
        self._pending[seq] = _PendingRequest(set(expected), future, handle)

        try:
            await self._send_packet_internal(
                cmd,
                payload,
                seq=seq,
                require_authed=require_authed,
            )
            return await future
        finally:
            # A caller can be cancelled either while sending or while awaiting
            # a server receipt. Do not retain its pending slot or timeout
            # callback until the normal deadline in either phase.
            pending = self._pending.pop(seq, None)
            if pending is not None:
                pending.timeout_handle.cancel()
                if not pending.future.done():
                    pending.future.cancel()

    async def send_text(
        self,
        session_id: str,
        text: str,
        *,
        reply_to_message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        event_id: Optional[str] = None,
        biz_card: Optional[Dict[str, Any]] = None,
        channel_data: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        stripped_session = session_id.strip()
        payload: Dict[str, Any] = {
            "session_id": stripped_session,
            "msg_type": 1,
            "content": text,
        }
        # client_msg_id 必须每条唯一：event_id 现在随每条过程/最终消息一起上送
        # （服务端据此继承触发消息的 visible_to），不能再用 event_id 充当去重键。
        digest = hashlib.sha256(
            f"{stripped_session}:{time.monotonic_ns()}".encode()
        ).hexdigest()[:16]
        payload["client_msg_id"] = f"hermes_{digest}"
        if reply_to_message_id:
            payload["quoted_message_id"] = reply_to_message_id.strip()
        if thread_id:
            payload["thread_id"] = thread_id.strip()
        if event_id:
            payload["event_id"] = event_id.strip()
        if isinstance(biz_card, dict) and biz_card:
            payload["biz_card"] = biz_card
        if isinstance(channel_data, dict) and channel_data:
            payload["channel_data"] = channel_data

        packet = await self.request(
            CMD_SEND_MSG,
            payload,
            expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
            timeout_ms=timeout_ms,
        )
        if packet["cmd"] != CMD_SEND_ACK:
            raise self._packet_error(packet)
        return {
            "ok": True,
            "message_id": (
                str(packet["payload"].get("msg_id") or packet["payload"].get("client_msg_id") or "").strip()
                or None
            ),
            "packet": packet,
        }

    async def send_media(
        self,
        session_id: str,
        content: str,
        extra: Dict[str, Any],
        *,
        reply_to_message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        event_id: Optional[str] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        stripped_session = session_id.strip()
        if event_id:
            client_msg_id = f"hermes_media_{event_id.strip()}"
        else:
            digest = hashlib.sha256(
                f"{stripped_session}:{time.monotonic_ns()}".encode()
            ).hexdigest()[:16]
            client_msg_id = f"hermes_media_{digest}"

        payload: Dict[str, Any] = {
            "session_id": stripped_session,
            "msg_type": 2,
            "content": content,
            "client_msg_id": client_msg_id,
            "extra": extra,
        }
        if reply_to_message_id:
            payload["quoted_message_id"] = reply_to_message_id.strip()
        if thread_id:
            payload["thread_id"] = thread_id.strip()
        if event_id:
            payload["event_id"] = event_id.strip()

        packet = await self.request(
            CMD_SEND_MSG,
            payload,
            expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
            timeout_ms=timeout_ms,
        )
        if packet["cmd"] != CMD_SEND_ACK:
            raise self._packet_error(packet)
        return {
            "ok": True,
            "message_id": (
                str(packet["payload"].get("msg_id") or packet["payload"].get("client_msg_id") or "").strip()
                or None
            ),
            "packet": packet,
        }

    async def edit_message(
        self,
        session_id: str,
        message_id: str,
        text: str,
        *,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        packet = await self.request(
            CMD_EDIT_MSG,
            {
                "session_id": session_id.strip(),
                "msg_id": message_id.strip(),
                "content": text,
            },
            expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
            timeout_ms=timeout_ms,
        )
        if packet["cmd"] != CMD_SEND_ACK:
            raise self._packet_error(packet)
        return {
            "ok": True,
            "session_id": str(packet["payload"].get("session_id") or session_id).strip(),
            "message_id": str(packet["payload"].get("msg_id") or message_id).strip(),
            "packet": packet,
        }

    async def set_session_activity(
        self,
        *,
        session_id: str,
        kind: str,
        active: bool,
        ttl_ms: Optional[int] = None,
        ref_message_id: Optional[str] = None,
        ref_event_id: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "kind": kind,
            "active": active,
        }
        if ttl_ms is not None:
            payload["ttl_ms"] = int(ttl_ms)
        if ref_message_id:
            payload["ref_msg_id"] = ref_message_id.strip()
        if ref_event_id:
            payload["ref_event_id"] = ref_event_id.strip()
        await self.send_packet(CMD_SESSION_ACTIVITY_SET, payload)

    async def send_local_action_result(
        self,
        *,
        action_id: str,
        status: str,
        result: Optional[Any] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "action_id": action_id.strip(),
            "status": status.strip(),
        }
        if result is not None:
            payload["result"] = result
        if error_code:
            payload["error_code"] = error_code.strip()
        if error_message:
            payload["error_msg"] = error_message.strip()
        await self.send_packet(CMD_LOCAL_ACTION_RESULT, payload)

    async def send_local_action_result_confirmed(
        self,
        *,
        action_id: str,
        status: str,
        result: Optional[Any] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> bool:
        """Send a local-action result and wait for server receipt when negotiated.

        The confirmation gate is used before a self-restart: a local socket
        write does not prove that the gateway persisted the result.  Older
        servers remain compatible but return ``False`` so callers must not
        restart based on an unconfirmed result.
        """
        payload: Dict[str, Any] = {"action_id": action_id.strip(), "status": status.strip()}
        if result is not None:
            payload["result"] = result
        if error_code:
            payload["error_code"] = error_code.strip()
        if error_message:
            payload["error_msg"] = error_message.strip()
        if "local_action_result_ack" not in self._negotiated_capabilities:
            await self.send_packet(CMD_LOCAL_ACTION_RESULT, payload)
            return False
        packet = await self.request(
            CMD_LOCAL_ACTION_RESULT,
            payload,
            expected=(CMD_LOCAL_ACTION_ACK, CMD_ERROR),
        )
        if packet["cmd"] != CMD_LOCAL_ACTION_ACK:
            raise self._packet_error(packet)
        ack_action_id = str((packet.get("payload") or {}).get("action_id") or "").strip()
        if ack_action_id != action_id.strip() or (packet.get("payload") or {}).get("received") is not True:
            raise GrixTransportError("invalid local_action_result acknowledgement")
        return True

    async def request_relay_credential(
        self,
        *,
        model: str,
        openai_base_url: str,
        anthropic_base_url: str,
    ) -> Dict[str, Any]:
        """Request an agent-scoped relay credential without logging its value."""
        packet = await self.request(
            CMD_RELAY_CREDENTIAL_REQUEST,
            {
                "model": model,
                "openai_base_url": openai_base_url,
                "anthropic_base_url": anthropic_base_url,
            },
            expected=(CMD_RELAY_CREDENTIAL_RESULT, CMD_ERROR),
        )
        if packet["cmd"] != CMD_RELAY_CREDENTIAL_RESULT:
            raise self._packet_error(packet)
        payload = packet.get("payload") or {}
        if payload.get("status") != "ok":
            raise GrixTransportError("relay credential request was rejected")
        return payload

    async def request_relay_state_sync(
        self,
        *,
        local_enabled: bool,
        local_model: Optional[str],
    ) -> Dict[str, Any]:
        """Fetch the desired relay state after a successful WS authentication."""
        payload: Dict[str, Any] = {"local_enabled": bool(local_enabled)}
        if local_model:
            payload["local_model"] = local_model
        packet = await self.request(
            CMD_RELAY_STATE_SYNC_REQUEST,
            payload,
            expected=(CMD_RELAY_STATE_SYNC_RESULT, CMD_ERROR),
        )
        if packet["cmd"] != CMD_RELAY_STATE_SYNC_RESULT:
            raise self._packet_error(packet)
        response = packet.get("payload") or {}
        if response.get("status") != "ok":
            raise GrixTransportError("relay state sync was rejected")
        return response

    async def send_relay_state_report(
        self,
        *,
        applied: bool,
        revision: int,
        error_code: Optional[str] = None,
    ) -> None:
        """Report the actual local relay state; errors never contain credentials."""
        payload: Dict[str, Any] = {"applied": bool(applied), "revision": int(revision)}
        if error_code:
            payload["error_code"] = error_code
        await self.send_packet(CMD_RELAY_STATE_REPORT, payload)

    async def acknowledge_event(
        self,
        *,
        event_id: str,
        session_id: Optional[str] = None,
        message_id: Optional[str] = None,
        received_at: Optional[int] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "event_id": event_id.strip(),
            "received_at": received_at or _now_ms(),
        }
        if session_id:
            payload["session_id"] = session_id.strip()
        if message_id:
            payload["msg_id"] = message_id.strip()
        await self.send_packet(CMD_EVENT_ACK, payload)

    async def complete_event(
        self,
        *,
        event_id: str,
        status: str,
        code: Optional[str] = None,
        message: Optional[str] = None,
        updated_at: Optional[int] = None,
        terminal_commit_token: Optional[str] = None,
    ) -> None:
        """Persist-then-send terminal event_result (connector outbox semantics).

        Returns after the durable enqueue schedules delivery; ACK completion is
        asynchronous. Callers that need fire-and-forget compatibility should not
        assume the network write has finished when this returns.
        """
        payload: Dict[str, Any] = {
            "event_id": event_id.strip(),
            "status": status,
            "updated_at": updated_at or _now_ms(),
        }
        if code:
            payload["code"] = code.strip()
        if message:
            payload["msg"] = message.strip()
        if terminal_commit_token:
            payload["terminal_commit_token"] = terminal_commit_token.strip()
        self._terminal.send_event_result(payload)

    def capture_inbound_terminal_commit_token(
        self, event_id: Optional[str], raw_token: Optional[str]
    ) -> bool:
        return self._terminal.capture_inbound_terminal_commit_token(event_id, raw_token)

    def replay_terminal_outboxes(self) -> None:
        self._terminal.replay_terminal_outbox()
        self._terminal.replay_stop_result_outbox()

    async def acknowledge_stop(
        self,
        *,
        event_id: str,
        accepted: bool,
        stop_id: Optional[str] = None,
        updated_at: Optional[int] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "event_id": event_id.strip(),
            "accepted": accepted,
            "updated_at": updated_at or _now_ms(),
        }
        if stop_id:
            payload["stop_id"] = stop_id.strip()
        await self.send_packet(CMD_EVENT_STOP_ACK, payload)

    async def complete_stop(
        self,
        *,
        event_id: str,
        status: str,
        stop_id: Optional[str] = None,
        code: Optional[str] = None,
        message: Optional[str] = None,
        updated_at: Optional[int] = None,
        terminal_commit_token: Optional[str] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "event_id": event_id.strip(),
            "status": status,
            "updated_at": updated_at or _now_ms(),
        }
        if stop_id:
            payload["stop_id"] = stop_id.strip()
        if code:
            payload["code"] = code.strip()
        if message:
            payload["msg"] = message.strip()
        if terminal_commit_token:
            payload["terminal_commit_token"] = terminal_commit_token.strip()
        self._terminal.send_event_stop_result(payload)

    async def send_event_cancel_result(
        self,
        *,
        event_id: str,
        accepted: bool,
        reason: Optional[str] = None,
        final_state: Optional[str] = None,
    ) -> None:
        """对服务端 event_cancel 请求的回应。

        accepted=True 表示已接受取消；final_state 为事件的最终生命周期状态
        （如 canceled）；reason 解释拒绝或失败原因。
        """
        payload: Dict[str, Any] = {
            "event_id": event_id.strip(),
            "accepted": accepted,
        }
        if final_state:
            payload["final_state"] = final_state.strip()
        if reason:
            payload["reason"] = reason.strip()
        await self.send_packet(CMD_EVENT_CANCEL_RESULT, payload)

    async def send_queue_clear_result(
        self,
        *,
        session_id: str,
        success: bool,
        canceled_event_ids: Optional[List[str]] = None,
        message: Optional[str] = None,
    ) -> None:
        """对服务端 queue_clear 请求的回应（对齐 connector：带被取消的事件 id 列表）。"""
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "success": success,
            "canceled_event_ids": list(canceled_event_ids or []),
        }
        if message:
            payload["msg"] = message.strip()
        await self.send_packet(CMD_QUEUE_CLEAR_RESULT, payload)

    async def send_event_hold_result(
        self,
        *,
        session_id: str,
        event_id: str,
        ok: bool,
        held: bool = False,
        error: Optional[str] = None,
    ) -> None:
        """对服务端 event_hold 请求的回应。

        ok=True 表示 hold/release 已生效，held 为该事件当前的持有态；
        失败时 error 为枚举（not_found / bad_request）。
        """
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "event_id": event_id.strip(),
            "ok": ok,
            "held": held,
            "error": (error or "").strip(),
        }
        await self.send_packet(CMD_EVENT_HOLD_RESULT, payload)

    async def send_queue_edit_result(
        self,
        *,
        session_id: str,
        event_id: str,
        ok: bool,
        error: Optional[str] = None,
    ) -> None:
        """对服务端 queue_edit 请求的回应。

        失败时 error 为枚举（not_found / bad_request / empty_content）。
        """
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "event_id": event_id.strip(),
            "ok": ok,
            "error": (error or "").strip(),
        }
        await self.send_packet(CMD_QUEUE_EDIT_RESULT, payload)

    async def send_queue_reorder_result(
        self,
        *,
        session_id: str,
        applied_event_ids: List[str],
    ) -> None:
        """对服务端 queue_reorder 请求的回应：应用后的实际排队顺序（队头在前）。"""
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "applied_event_ids": list(applied_event_ids),
        }
        await self.send_packet(CMD_QUEUE_REORDER_RESULT, payload)

    async def send_event_state(
        self,
        *,
        event_id: str,
        session_id: str,
        state: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        """主动上报某个事件的当前状态，由后端透传给 APP 端。"""
        payload: Dict[str, Any] = {
            "event_id": event_id.strip(),
            "session_id": session_id.strip(),
            "state": state.strip(),
            "updated_at": _now_ms(),
        }
        if extra:
            payload.update(extra)
        await self.send_packet(CMD_EVENT_STATE, payload)

    async def send_audit_state(self, payload: Dict[str, Any]) -> None:
        """Report audit lifecycle metadata; replay content stays local."""
        await self.send_packet(
            CMD_AUDIT_STATE,
            {key: value for key, value in payload.items() if value is not None},
        )

    async def send_queue_snapshot(
        self,
        *,
        session_id: str,
        running: List[str],
        running_items: List[Dict[str, Any]],
        queued: List[Dict[str, Any]],
    ) -> None:
        """主动上报会话事件队列快照，由后端透传给 APP 端（对齐 connector 载荷结构）。

        running 为正在执行的事件 id 列表；running_items / queued 为对应事件
        描述（event_id、content_preview、position、actions 等）。
        """
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "running": list(running),
            "running_items": list(running_items),
            "queued": list(queued),
            "updated_at": _now_ms(),
        }
        await self.send_packet(CMD_QUEUE_SNAPSHOT, payload)

    async def send_update_binding_card(
        self,
        *,
        session_id: str,
        worker_status: str,
        cwd: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist metadata used to build this session's toolbar snapshot."""
        payload: Dict[str, Any] = {
            "session_id": session_id.strip(),
            "worker_status": worker_status.strip(),
            "cwd": cwd.strip(),
        }
        if meta:
            payload["meta"] = dict(meta)
        await self.send_packet(CMD_UPDATE_BINDING_CARD, payload)

    async def bind_session_route(
        self,
        *,
        channel: str,
        account_id: str,
        route_session_key: str,
        session_id: str,
        timeout_ms: Optional[int] = None,
    ) -> None:
        packet = await self.request(
            CMD_SESSION_ROUTE_BIND,
            {
                "channel": channel.strip(),
                "account_id": account_id.strip(),
                "route_session_key": route_session_key.strip(),
                "session_id": session_id.strip(),
            },
            expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
            timeout_ms=timeout_ms,
        )
        if packet["cmd"] != CMD_SEND_ACK:
            raise self._packet_error(packet)

    async def resolve_session_route(
        self,
        *,
        channel: str,
        account_id: str,
        route_session_key: str,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        packet = await self.request(
            CMD_SESSION_ROUTE_RESOLVE,
            {
                "channel": channel.strip(),
                "account_id": account_id.strip(),
                "route_session_key": route_session_key.strip(),
            },
            expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
            timeout_ms=timeout_ms,
        )
        if packet["cmd"] != CMD_SEND_ACK:
            raise self._packet_error(packet)

        session_id = str(packet["payload"].get("session_id") or "").strip()
        if not session_id:
            raise GrixTransportError("session_route_resolve returned empty session_id")
        return {
            "channel": str(packet["payload"].get("channel") or "").strip(),
            "account_id": str(packet["payload"].get("account_id") or "").strip(),
            "route_session_key": str(packet["payload"].get("route_session_key") or "").strip(),
            "session_id": session_id,
        }

    async def agent_invoke(
        self,
        *,
        action: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "invoke_id": uuid.uuid4().hex,
            "action": action.strip(),
            "timeout_ms": timeout_ms or 15_000,
        }
        if params:
            payload["params"] = params

        packet = await self.request(
            CMD_AGENT_INVOKE,
            payload,
            expected=(CMD_AGENT_INVOKE_RESULT, CMD_ERROR),
            timeout_ms=timeout_ms or 30_000,
        )
        result_payload = packet["payload"]
        code = parse_code(result_payload)
        if code != 0:
            raise GrixPacketError(packet["cmd"], code, parse_message(result_payload))
        return result_payload

    async def send_skills_update(
        self,
        skills: List[Dict[str, Any]],
        *,
        library_skills: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """主动上报 skills（+ library_skills），后端整体覆盖 runtime profile。"""
        payload: Dict[str, Any] = {"skills": skills}
        if library_skills is not None:
            payload["library_skills"] = library_skills
        await self.send_packet("agent_skills_update", payload)

    async def _reader_loop(self) -> None:
        try:
            while self._socket:
                frame = await self._socket.receive()
                kind = frame.get("kind")
                if kind == "text":
                    await self._handle_packet_text(frame.get("text", ""))
                    continue
                if kind == "closed":
                    raise GrixConnectionClosedError(frame.get("reason") or "grix websocket closed")
                raise GrixConnectionClosedError("grix websocket error")
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._disconnect_requested:
                return
            await self.disconnect(str(exc))

    async def _handle_packet_text(self, text: str) -> None:
        if not text:
            return
        packet = decode_packet(text)
        if packet["cmd"] == CMD_PING:
            await self._send_packet_internal(
                CMD_PONG,
                {"ts": _now_ms()},
                seq=packet["seq"] if packet["seq"] > 0 else None,
                require_authed=False,
            )
            return

        pending = self._pending.get(packet["seq"])
        if pending and packet["cmd"] in pending.expected:
            self._pending.pop(packet["seq"], None)
            pending.timeout_handle.cancel()
            if not pending.future.done():
                pending.future.set_result(packet)
            return

        if self.on_packet:
            task = asyncio.create_task(self._run_on_packet(packet))
            self._packet_tasks.add(task)
            task.add_done_callback(self._packet_tasks.discard)

    async def _run_on_packet(self, packet: Dict[str, Any]) -> None:
        if not self.on_packet:
            return
        try:
            await _maybe_await(self.on_packet(packet))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("GRIX packet handler failed for %s", packet.get("cmd"))

    async def _heartbeat_loop(self, heartbeat_sec: int) -> None:
        interval = max(heartbeat_sec, 5)
        failures = 0
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self.request(
                        "ping",
                        {"ts": _now_ms()},
                        expected=("pong",),
                        timeout_ms=min(interval * 1000, 15_000),
                    )
                    failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self._disconnect_requested:
                        return
                    # 单次 pong 超时可能只是链路拥塞（如重连补发洪峰把心跳挤在队列后），
                    # 连续两次才判死，避免误杀健康但拥塞的连接形成断连-补发循环。
                    failures += 1
                    if failures >= 2:
                        await self.disconnect(f"heartbeat failed: {exc}")
                        return
        except asyncio.CancelledError:
            return

    async def _send_packet_internal(
        self,
        cmd: str,
        payload: Dict[str, Any],
        *,
        seq: Optional[int],
        require_authed: bool,
    ) -> int:
        self._ensure_ready(require_authed=require_authed)
        out_seq = seq or self._next_seq()
        packet = build_packet(cmd, payload, out_seq)
        async with self._send_lock:
            self._ensure_ready(require_authed=require_authed)
            socket = self._socket
            if not socket:
                raise GrixTransportError("grix websocket is not connected")
            try:
                await socket.send_text(encode_packet(packet))
            except Exception as exc:
                await self.disconnect(f"{cmd} send failed: {exc}")
                raise GrixConnectionClosedError(str(exc) or f"{cmd} send failed") from exc
        return out_seq

    def _ensure_ready(self, *, require_authed: bool) -> None:
        if not self._socket or not self._status["connected"]:
            raise GrixTransportError("grix websocket is not connected")
        if require_authed and not self._status["authed"]:
            raise GrixTransportError("grix websocket is not authenticated")

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _packet_error(self, packet: Dict[str, Any]) -> GrixPacketError:
        return GrixPacketError(
            packet["cmd"],
            parse_code(packet["payload"]),
            parse_message(packet["payload"]),
        )

    def _reject_pending(self, error: Exception) -> None:
        for seq, pending in list(self._pending.items()):
            pending.timeout_handle.cancel()
            if not pending.future.done():
                pending.future.set_exception(error)
            self._pending.pop(seq, None)

    def _update_status(self, patch: Dict[str, Any]) -> None:
        self._status.update(patch)
        if self.on_status:
            result = self.on_status(dict(self._status))
            if asyncio.iscoroutine(result):
                task = asyncio.create_task(result)
                self._status_tasks.add(task)
                task.add_done_callback(self._status_tasks.discard)
