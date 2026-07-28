"""Reliable terminal event_result / event_stop_result delivery (connector parity).

Backend deploy constraint: roll out ``terminal_commit_v1`` / stop-token support
before this Hermes version. Do not invent a parallel protocol.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any, Dict, Optional, TYPE_CHECKING

from .contract import (
    CAP_TERMINAL_COMMIT_V1,
    CMD_ERROR,
    CMD_SEND_ACK,
    CMD_SEND_NACK,
)
from .persistence import (
    StopResultOutbox,
    StopResultOutboxEntry,
    TerminalCommitTokenStore,
    TerminalCommittedStore,
    TerminalOutbox,
    TerminalOutboxEntry,
    TerminalRejection,
)
from .terminal_paths import resolve_terminal_sidecar_paths

if TYPE_CHECKING:
    from .transport import GrixTransportClient

logger = logging.getLogger(__name__)

TERMINAL_RETRY_DELAY_MS = 15_000
MAX_COMMITTED_TERMINAL_EVENTS = 4096


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


class TerminalDeliveryController:
    def __init__(self, client: "GrixTransportClient"):
        self._client = client
        config = client._config
        outbox_path, token_path, stop_path, committed_path = resolve_terminal_sidecar_paths(
            getattr(config, "terminal_outbox_path", None),
            token_path=getattr(config, "terminal_commit_token_store_path", None),
            stop_path=getattr(config, "stop_result_outbox_path", None),
            committed_path=getattr(config, "terminal_committed_store_path", None),
        )
        self.terminal_outbox = TerminalOutbox(outbox_path)
        self.terminal_commit_tokens = TerminalCommitTokenStore(token_path)
        self.stop_result_outbox = StopResultOutbox(stop_path)
        self.terminal_committed = TerminalCommittedStore(
            committed_path, max_size=MAX_COMMITTED_TERMINAL_EVENTS
        )

        self._provisional_responded: Dict[str, Dict[str, Any]] = {}
        self._terminal_in_flight: set[str] = set()
        self._terminal_replay_after_flight: set[str] = set()
        self._terminal_retry_handles: Dict[str, asyncio.TimerHandle] = {}
        self._stop_in_flight: set[str] = set()
        self._stop_retry_handles: Dict[str, asyncio.TimerHandle] = {}
        self._delivery_tasks: set[asyncio.Task] = set()

    def is_terminal_settled(self, event_id: str) -> bool:
        return self.terminal_committed.has(event_id)

    # --- inbound token capture -------------------------------------------------

    def capture_inbound_terminal_commit_token(
        self, event_id: Optional[str], raw_token: Optional[str]
    ) -> bool:
        token = str(raw_token or "").strip()
        if not token:
            return True
        eid = str(event_id or "").strip()
        if not eid:
            self._reject_inbound_token("tokenized event is missing event_id")
            return False
        if CAP_TERMINAL_COMMIT_V1 not in self._client.negotiated_capabilities:
            self._reject_inbound_token(
                f"tokenized event received without terminal_commit_v1 negotiation event={eid}"
            )
            return False
        try:
            self.terminal_commit_tokens.register(eid, token)
            return True
        except Exception as exc:
            self._reject_inbound_token(
                f"terminal commit token persist failed event={eid}: {exc}"
            )
            return False

    def _reject_inbound_token(self, reason: str) -> None:
        logger.error("grix terminal token rejected: %s", reason)
        socket = self._client._socket
        if socket is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(socket.close("terminal commit token persistence failed"))
            except RuntimeError:
                pass

    def with_terminal_commit_token(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        event_id = str(payload.get("event_id") or "").strip()
        if not event_id:
            raise ValueError("terminal event_result is missing event_id")
        supplied = str(payload.get("terminal_commit_token") or "").strip()
        persisted = self.terminal_commit_tokens.get(event_id)
        if supplied and persisted and supplied != persisted:
            raise ValueError(f"terminal commit token mismatch for event={event_id}")
        if supplied and not persisted:
            self.terminal_commit_tokens.register(event_id, supplied)
        token = persisted or supplied
        out = {**payload, "event_id": event_id}
        if token:
            out["terminal_commit_token"] = token
        return out

    # --- lifecycle -------------------------------------------------------------

    def on_authenticated(self) -> None:
        self.replay_terminal_outbox()
        self.replay_stop_result_outbox()

    def on_disconnect(self) -> None:
        self.clear_terminal_retry_timers()
        self.clear_stop_result_retry_timers()
        self._provisional_responded.clear()
        for task in list(self._delivery_tasks):
            task.cancel()
        self._delivery_tasks.clear()
        self._terminal_in_flight.clear()
        self._stop_in_flight.clear()

    # --- outbound event_result -------------------------------------------------

    def send_event_result(self, payload: Dict[str, Any]) -> None:
        try:
            payload = self.with_terminal_commit_token(payload)
        except Exception as exc:
            logger.error(
                "event_result token validation failed: event=%s: %s",
                payload.get("event_id"),
                exc,
            )
            return

        event_id = str(payload.get("event_id") or "")
        durable = self.terminal_outbox.get(event_id)
        if self.terminal_committed.has(event_id) and not durable:
            logger.info(
                "ignored duplicate terminal after committed ACK event=%s status=%s",
                event_id,
                payload.get("status"),
            )
            return

        if durable:
            replace_guard = self.can_replace_unsent_output_guard(durable) and payload.get(
                "status"
            ) in ("canceled", "failed")
            if not replace_guard:
                logger.info(
                    "preserving first durable terminal event=%s existing_status=%s ignored_status=%s",
                    event_id,
                    durable.payload.get("status"),
                    payload.get("status"),
                )
                self.schedule_terminal_delivery(event_id, ignore_not_before=True)
                return
            self._provisional_responded.pop(event_id, None)

        self._provisional_responded.pop(event_id, None)
        try:
            entry = self.terminal_outbox.enqueue(payload)
        except Exception as exc:
            logger.error(
                "event_result outbox persist failed: event=%s status=%s: %s",
                event_id,
                payload.get("status"),
                exc,
            )
            return
        self.schedule_terminal_delivery(entry.payload["event_id"], ignore_not_before=True)

    def send_event_stop_result(self, payload: Dict[str, Any]) -> None:
        event_id = str(payload.get("event_id") or "").strip()
        supplied = str(payload.get("terminal_commit_token") or "").strip()
        persisted = self.terminal_commit_tokens.get(event_id) if event_id else None
        if supplied and persisted and supplied != persisted:
            logger.error("event_stop_result token mismatch event=%s", event_id)
            return
        terminal_commit_token = persisted or supplied
        status = str(payload.get("status") or "")
        if event_id and terminal_commit_token and status in ("stopped", "already_finished"):
            try:
                entry = self.stop_result_outbox.enqueue(
                    {
                        **payload,
                        "event_id": event_id,
                        "terminal_commit_token": terminal_commit_token,
                    }
                )
                self.schedule_stop_result_delivery(entry, ignore_not_before=True)
            except Exception as exc:
                logger.error(
                    "event_stop_result outbox persist failed: event=%s stop=%s: %s",
                    event_id,
                    payload.get("stop_id"),
                    exc,
                )
            return
        # Non-durable stop results stay fire-and-forget.
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self._client.send_packet("event_stop_result", payload)
        )
        self._track_task(task)

    # --- scheduling / delivery -------------------------------------------------

    def replay_terminal_outbox(self) -> None:
        for entry in self.terminal_outbox.list_pending():
            self.schedule_terminal_delivery(
                entry.payload["event_id"], ignore_not_before=True
            )

    def schedule_terminal_delivery(
        self,
        event_id: str,
        *,
        ignore_not_before: bool = False,
        minimum_delay_ms: int = 0,
    ) -> None:
        if not self._client.is_ready_for_outbound:
            return
        if event_id in self._terminal_in_flight:
            if ignore_not_before:
                self._terminal_replay_after_flight.add(event_id)
            return
        entry = self.terminal_outbox.get(event_id)
        if not entry:
            return
        existing = self._terminal_retry_handles.pop(event_id, None)
        if existing:
            existing.cancel()

        persisted_delay = (
            0 if ignore_not_before else max(0, entry.next_attempt_at - _now_ms())
        )
        delay_ms = max(persisted_delay, minimum_delay_ms)
        if delay_ms > 0:
            loop = asyncio.get_running_loop()
            handle = loop.call_later(
                delay_ms / 1000,
                lambda: self._on_terminal_retry_timer(event_id),
            )
            self._terminal_retry_handles[event_id] = handle
            return
        self._spawn(self._deliver_terminal_entry(entry))

    def _on_terminal_retry_timer(self, event_id: str) -> None:
        self._terminal_retry_handles.pop(event_id, None)
        self.schedule_terminal_delivery(event_id, ignore_not_before=True)

    async def _deliver_terminal_entry(self, entry: TerminalOutboxEntry) -> None:
        event_id = str(entry.payload.get("event_id") or "")
        generation = self._client.connection_generation
        if event_id in self._terminal_in_flight or not self.terminal_outbox.is_current(entry):
            return
        self._terminal_in_flight.add(event_id)
        minimum_delay_ms = 0
        try:
            if not self._client.is_connection_current(generation):
                return
            minimum_delay_ms = await self._send_event_result_reliable(entry, generation)
        finally:
            self._terminal_in_flight.discard(event_id)
            replay = event_id in self._terminal_replay_after_flight
            self._terminal_replay_after_flight.discard(event_id)
            if self.terminal_outbox.get(event_id) and self._client.is_connection_current(
                generation
            ):
                self.schedule_terminal_delivery(
                    event_id,
                    ignore_not_before=replay,
                    minimum_delay_ms=0 if replay else minimum_delay_ms,
                )

    async def _send_event_result_reliable(
        self, entry: TerminalOutboxEntry, generation: int
    ) -> int:
        event_id = str(entry.payload.get("event_id") or "")
        token = str(entry.payload.get("terminal_commit_token") or "").strip()
        if token and CAP_TERMINAL_COMMIT_V1 not in self._client.negotiated_capabilities:
            logger.error(
                "refusing tokenized event_result on an unnegotiated connection: event=%s",
                event_id,
            )
            await self._client.reconnect_after_outbound_failure(
                "terminal_commit_v1 not negotiated"
            )
            return TERMINAL_RETRY_DELAY_MS

        provisional = self._provisional_responded.get(event_id)
        if provisional and self.terminal_outbox.is_current(entry):
            if not self.can_replace_unsent_output_guard(entry):
                self._provisional_responded.pop(event_id, None)
            else:
                try:
                    self.terminal_outbox.enqueue(provisional)
                    self._provisional_responded.pop(event_id, None)
                    return 0
                except Exception as exc:
                    logger.error(
                        "failed to promote confirmed output terminal to responded: event=%s: %s",
                        event_id,
                        exc,
                    )
                    return TERMINAL_RETRY_DELAY_MS

        try:
            if not self.terminal_outbox.mark_delivery_started(entry):
                return 0
        except Exception as exc:
            logger.error(
                "event_result delivery-start persist failed: event=%s: %s", event_id, exc
            )
            return TERMINAL_RETRY_DELAY_MS

        ack_policy = self._client.ack_policy or {}
        max_attempts = max(1, int(ack_policy.get("max_retries") or 3))
        timeout_ms = int(ack_policy.get("push_ack_timeout_ms") or 5_000)
        retry_delay_ms = 750
        last_error = "unknown delivery failure"
        payload = entry.payload

        for attempt in range(1, max_attempts + 1):
            if not self._client.is_connection_current(generation):
                return TERMINAL_RETRY_DELAY_MS
            if not self.terminal_outbox.is_current(entry):
                return 0
            logger.info(
                "event_result send attempt event=%s status=%s attempt=%s/%s",
                event_id,
                payload.get("status"),
                attempt,
                max_attempts,
            )
            try:
                packet = await self._client.request(
                    "event_result",
                    payload,
                    expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
                    timeout_ms=timeout_ms,
                )
                if not self._client.is_connection_current(generation):
                    return TERMINAL_RETRY_DELAY_MS
                if not self.terminal_outbox.is_current(entry):
                    return 0
                if packet["cmd"] == CMD_SEND_ACK:
                    ack = packet.get("payload") or {}
                    if (
                        ack.get("event_id") != payload.get("event_id")
                        or ack.get("status") != payload.get("status")
                    ):
                        raise RuntimeError(
                            f"event_result ACK mismatch: expected event={payload.get('event_id')} "
                            f"status={payload.get('status')}, got event={ack.get('event_id')} "
                            f"status={ack.get('status')}"
                        )
                    if token and (
                        str(ack.get("terminal_commit_token") or "").strip() != token
                        or ack.get("terminal_committed") is not True
                    ):
                        raise RuntimeError(
                            f"event_result terminal commit ACK mismatch: event={event_id}"
                        )
                    if token:
                        try:
                            self.terminal_commit_tokens.remove(event_id, token)
                        except Exception as exc:
                            raise RuntimeError(
                                f"terminal commit token cleanup failed: event={event_id}: {exc}"
                            ) from exc
                    if self.terminal_outbox.acknowledge(
                        entry,
                        ack.get("event_id"),
                        ack.get("status"),
                        ack.get("terminal_commit_token"),
                        ack.get("terminal_committed"),
                    ):
                        self.remember_committed_terminal_event(event_id)
                        self.schedule_stop_results_for_event(event_id)
                    return 0

                err = packet.get("payload") or {}
                if token:
                    last_error = (
                        f"tokenized terminal rejected: cmd={packet['cmd']} "
                        f"code={err.get('code')} msg={err.get('msg')}"
                    )
                    await self._client.reconnect_after_outbound_failure(
                        "tokenized terminal rejected"
                    )
                    break
                try:
                    moved = self.terminal_outbox.move_to_dead_letter(
                        entry,
                        TerminalRejection(
                            response_cmd=str(packet["cmd"]),
                            code=(
                                int(err["code"])
                                if isinstance(err.get("code"), (int, float))
                                else None
                            ),
                            message=(
                                str(err.get("msg"))
                                if err.get("msg") is not None
                                else None
                            ),
                        ),
                    )
                    if moved:
                        # Dead-letter is a settled terminal: seal against memory
                        # replay re-enqueue after client replacement.
                        self.remember_committed_terminal_event(event_id)
                        logger.error(
                            "event_result rejected and moved to dead-letter: event=%s "
                            "status=%s cmd=%s code=%s msg=%s",
                            event_id,
                            payload.get("status"),
                            packet["cmd"],
                            err.get("code"),
                            err.get("msg"),
                        )
                    return 0
                except Exception as persist_err:
                    last_error = f"dead-letter persist failed: {persist_err}"
                    break
            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "event_result attempt failed event=%s status=%s attempt=%s/%s err=%s",
                    event_id,
                    payload.get("status"),
                    attempt,
                    max_attempts,
                    last_error,
                )
                if attempt == max_attempts:
                    break
                await asyncio.sleep((retry_delay_ms * attempt) / 1000)

        next_attempt_at = _now_ms() + TERMINAL_RETRY_DELAY_MS
        try:
            self.terminal_outbox.record_retry(entry, next_attempt_at, last_error)
        except Exception as persist_err:
            logger.error(
                "event_result retry state persist failed: event=%s: %s",
                event_id,
                persist_err,
            )
        logger.error(
            "event_result ack failed after %s attempts; retained for retry: event=%s "
            "status=%s err=%s",
            max_attempts,
            event_id,
            payload.get("status"),
            last_error,
        )
        return TERMINAL_RETRY_DELAY_MS

    # --- stop result -----------------------------------------------------------

    def replay_stop_result_outbox(self) -> None:
        for entry in self.stop_result_outbox.list_pending():
            self.schedule_stop_result_delivery(entry, ignore_not_before=True)

    def schedule_stop_results_for_event(self, event_id: str) -> None:
        for entry in self.stop_result_outbox.list_pending():
            if entry.payload.get("event_id") == event_id:
                self.schedule_stop_result_delivery(entry, ignore_not_before=True)

    def schedule_stop_result_delivery(
        self, entry: StopResultOutboxEntry, *, ignore_not_before: bool = False
    ) -> None:
        if not self._client.is_ready_for_outbound or not self.stop_result_outbox.is_current(
            entry
        ):
            return
        event_id = str(entry.payload.get("event_id") or "")
        # Preserve event_result as the canonical terminal fence.
        if self.terminal_outbox.get(event_id) or self.terminal_commit_tokens.get(event_id):
            return
        existing = self._stop_retry_handles.pop(entry.key, None)
        if existing:
            existing.cancel()
        delay_ms = (
            0 if ignore_not_before else max(0, entry.next_attempt_at - _now_ms())
        )
        if delay_ms > 0:
            loop = asyncio.get_running_loop()
            handle = loop.call_later(
                delay_ms / 1000,
                lambda e=entry: self._on_stop_retry_timer(e),
            )
            self._stop_retry_handles[entry.key] = handle
            return
        self._spawn(self._deliver_stop_result_entry(entry))

    def _on_stop_retry_timer(self, entry: StopResultOutboxEntry) -> None:
        self._stop_retry_handles.pop(entry.key, None)
        current = None
        for pending in self.stop_result_outbox.list_pending():
            if pending.key == entry.key:
                current = pending
                break
        if current:
            self.schedule_stop_result_delivery(current, ignore_not_before=True)

    async def _deliver_stop_result_entry(self, entry: StopResultOutboxEntry) -> None:
        generation = self._client.connection_generation
        if entry.key in self._stop_in_flight or not self.stop_result_outbox.is_current(entry):
            return
        self._stop_in_flight.add(entry.key)
        retry_delay_ms = TERMINAL_RETRY_DELAY_MS
        try:
            if not self._client.is_connection_current(generation):
                return
            retry_delay_ms = await self._send_stop_result_reliable(entry, generation)
        finally:
            self._stop_in_flight.discard(entry.key)
            if self.stop_result_outbox.is_current(entry) and self._client.is_connection_current(
                generation
            ):
                loop = asyncio.get_running_loop()
                handle = loop.call_later(
                    retry_delay_ms / 1000,
                    lambda e=entry: self._on_stop_retry_timer(e),
                )
                self._stop_retry_handles[entry.key] = handle

    async def _send_stop_result_reliable(
        self, entry: StopResultOutboxEntry, generation: int
    ) -> int:
        timeout_ms = int((self._client.ack_policy or {}).get("push_ack_timeout_ms") or 5_000)
        last_error = "unknown delivery failure"
        try:
            packet = await self._client.request(
                "event_stop_result",
                entry.payload,
                expected=(CMD_SEND_ACK, CMD_SEND_NACK, CMD_ERROR),
                timeout_ms=timeout_ms,
            )
            if not self._client.is_connection_current(generation):
                return TERMINAL_RETRY_DELAY_MS
            if not self.stop_result_outbox.is_current(entry):
                return 0
            if packet["cmd"] == CMD_SEND_ACK:
                ack = packet.get("payload") or {}
                if self.stop_result_outbox.acknowledge(
                    entry,
                    ack.get("event_id"),
                    ack.get("terminal_commit_token"),
                    ack.get("terminal_committed"),
                ):
                    return 0
                last_error = (
                    f"event_stop_result ACK mismatch event={entry.payload.get('event_id')}"
                )
            else:
                err = packet.get("payload") or {}
                code = err.get("code")
                last_error = (
                    f"event_stop_result rejected: cmd={packet['cmd']} "
                    f"code={code} msg={err.get('msg')}"
                )
                if code in (4001, 4003):
                    try:
                        if self.stop_result_outbox.discard(entry):
                            logger.error(
                                "event_stop_result permanently rejected and discarded: "
                                "event=%s stop=%s code=%s msg=%s",
                                entry.payload.get("event_id"),
                                entry.payload.get("stop_id"),
                                code,
                                err.get("msg"),
                            )
                    except Exception as persist_err:
                        logger.error(
                            "event_stop_result discard persist failed: event=%s stop=%s: %s",
                            entry.payload.get("event_id"),
                            entry.payload.get("stop_id"),
                            persist_err,
                        )
                    return 0
        except Exception as exc:
            last_error = str(exc)

        next_attempt_at = _now_ms() + TERMINAL_RETRY_DELAY_MS
        try:
            self.stop_result_outbox.record_retry(entry, next_attempt_at, last_error)
        except Exception as persist_err:
            logger.error(
                "event_stop_result retry state persist failed: event=%s stop=%s: %s",
                entry.payload.get("event_id"),
                entry.payload.get("stop_id"),
                persist_err,
            )
        logger.error(
            "event_stop_result retained for retry: event=%s stop=%s err=%s",
            entry.payload.get("event_id"),
            entry.payload.get("stop_id"),
            last_error,
        )
        return TERMINAL_RETRY_DELAY_MS

    # --- helpers ---------------------------------------------------------------

    def can_replace_unsent_output_guard(self, entry: TerminalOutboxEntry) -> bool:
        return (
            entry.payload.get("code") == "agent_output_unconfirmed"
            and not entry.delivery_started_at
        )

    def remember_committed_terminal_event(self, event_id: str) -> None:
        self.terminal_committed.add(event_id)

    def clear_terminal_retry_timers(self) -> None:
        for handle in self._terminal_retry_handles.values():
            handle.cancel()
        self._terminal_retry_handles.clear()

    def clear_stop_result_retry_timers(self) -> None:
        for handle in self._stop_retry_handles.values():
            handle.cancel()
        self._stop_retry_handles.clear()

    def on_soft_disconnect(self) -> None:
        """Clear timers/provisional state without cancelling the caller task."""
        self.clear_terminal_retry_timers()
        self.clear_stop_result_retry_timers()
        self._provisional_responded.clear()
        self._terminal_in_flight.clear()
        self._stop_in_flight.clear()

    def _spawn(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(coro)
        self._track_task(task)

    def _track_task(self, task: asyncio.Task) -> None:
        self._delivery_tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._delivery_tasks.discard(done)
            with suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(_done)
