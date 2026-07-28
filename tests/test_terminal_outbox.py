"""终态投递持久化（terminal outbox）对齐 grix-connector 的聚焦单测。

覆盖：先落盘再发送、ACK 精确匹配才删除、持久化失败不触网、死信、
重连/进程替换重放、deliveryStartedAt freeze、stop outbox 与 4001/4003 discard。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.persistence import (  # noqa: E402
    StopResultOutbox,
    TerminalOutbox,
)
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402
from grix_hermes.terminal_paths import suffix_shared_path  # noqa: E402
from grix_hermes.transport import GrixTransportClient  # noqa: E402


class MockSocket:
    def __init__(self):
        self.sent: List[Dict[str, Any]] = []
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.on_send = None

    async def send_text(self, text: str) -> None:
        packet = json.loads(text)
        self.sent.append(packet)
        if self.on_send:
            maybe = self.on_send(packet)
            if asyncio.iscoroutine(maybe):
                await maybe

    async def receive(self) -> Dict[str, Any]:
        item = await self._inbox.get()
        if item.get("kind") == "closed":
            return item
        return item

    async def close(self, reason: str = "") -> None:
        self.closed = True
        await self._inbox.put({"kind": "closed", "reason": reason})

    def push(self, packet: Dict[str, Any]) -> None:
        self._inbox.put_nowait(
            {"kind": "text", "text": json.dumps(packet, ensure_ascii=False)}
        )


def _read_outbox(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _make_config(outbox: Path, **overrides) -> GrixConnectionConfig:
    base = {
        "endpoint": "ws://terminal-outbox.test",
        "agent_id": "agent-terminal",
        "api_key": "test-key",
        "terminal_outbox_path": str(outbox),
        "terminal_commit_token_store_path": str(outbox) + ".tokens",
        "stop_result_outbox_path": str(outbox) + ".stops",
    }
    base.update(overrides)
    return GrixConnectionConfig(**base)


async def _authenticate(
    client: GrixTransportClient,
    socket: MockSocket,
    *,
    ack_overrides: Optional[Dict[str, Any]] = None,
) -> None:
    connecting = asyncio.create_task(client.connect())

    async def wait_auth() -> Dict[str, Any]:
        for _ in range(200):
            auth = next((p for p in socket.sent if p["cmd"] == "auth"), None)
            if auth:
                return auth
            await asyncio.sleep(0.01)
        raise AssertionError("auth not sent")

    auth = await wait_auth()
    payload = {
        "code": 0,
        "msg": "ok",
        "heartbeat_sec": 60,
        "ack_policy": {"max_retries": 1, "push_ack_timeout_ms": 50},
        "supported_capabilities": ["terminal_commit_v1", "event_result_ack"],
    }
    if ack_overrides:
        payload.update(ack_overrides)
    socket.push({"cmd": "auth_ack", "seq": auth["seq"], "payload": payload})
    await connecting


def _latest_result(socket: MockSocket, event_id: str) -> Dict[str, Any]:
    matches = [
        p
        for p in socket.sent
        if p["cmd"] == "event_result" and p["payload"].get("event_id") == event_id
    ]
    assert matches, f"event_result not sent for {event_id}"
    return matches[-1]


def _respond(socket: MockSocket, request: Dict[str, Any], cmd: str, payload: Dict[str, Any]) -> None:
    socket.push({"cmd": cmd, "seq": request["seq"], "payload": payload})


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met before timeout")


def test_suffix_shared_path_isolates_owner():
    assert suffix_shared_path("/data/terminal-outbox-a.json", "42") == (
        "/data/terminal-outbox-a.shared.42.json"
    )


def test_terminal_outbox_atomic_enqueue_and_ack(tmp_path: Path | None = None):
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "outbox.json"
            socket = MockSocket()
            persisted_before_send = {"ok": False}

            def on_send(packet):
                if packet["cmd"] != "event_result":
                    return
                file = _read_outbox(outbox_path)
                persisted_before_send["ok"] = (
                    file["pending"]["evt-atomic"]["payload"]["status"] == "responded"
                )

            socket.on_send = on_send

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            await client.complete_event(event_id="evt-atomic", status="responded", message="done")
            await _wait_until(
                lambda: any(
                    p["cmd"] == "event_result" and p["payload"].get("event_id") == "evt-atomic"
                    for p in socket.sent
                )
            )
            assert persisted_before_send["ok"] is True
            request = _latest_result(socket, "evt-atomic")
            _respond(
                socket,
                request,
                "send_ack",
                {"event_id": "evt-atomic", "status": "responded"},
            )
            await _wait_until(lambda: "evt-atomic" not in _read_outbox(outbox_path)["pending"])
            await client.disconnect()

    asyncio.run(_run())


def test_ack_mismatch_retains_pending():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "mismatch.json"
            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            await client.complete_event(event_id="evt-mismatch", status="failed", message="boom")
            await _wait_until(
                lambda: any(p["cmd"] == "event_result" for p in socket.sent)
            )
            request = _latest_result(socket, "evt-mismatch")
            _respond(
                socket,
                request,
                "send_ack",
                {"event_id": "evt-other", "status": "failed"},
            )
            await asyncio.sleep(0.08)
            assert _read_outbox(outbox_path)["pending"]["evt-mismatch"]["payload"]["status"] == (
                "failed"
            )
            await client.disconnect()

    asyncio.run(_run())


def test_persist_failure_does_not_touch_network():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "cannot-replace-directory"
            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            outbox_path.mkdir()
            before = len([p for p in socket.sent if p["cmd"] == "event_result"])
            await client.complete_event(event_id="evt-persist-failure", status="failed")
            await asyncio.sleep(0.05)
            after = len([p for p in socket.sent if p["cmd"] == "event_result"])
            assert after == before
            await client.disconnect()

    asyncio.run(_run())


def test_nack_moves_to_dead_letter():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "nack.json"
            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            await client.complete_event(event_id="evt-nack", status="canceled")
            await _wait_until(lambda: any(p["cmd"] == "event_result" for p in socket.sent))
            request = _latest_result(socket, "evt-nack")
            _respond(socket, request, "send_nack", {"code": 4003, "msg": "ownership denied"})
            await _wait_until(lambda: "evt-nack" not in _read_outbox(outbox_path)["pending"])
            file = _read_outbox(outbox_path)
            assert file["deadLetters"][-1]["payload"]["event_id"] == "evt-nack"
            assert file["deadLetters"][-1]["responseCmd"] == "send_nack"
            await client.disconnect()

    asyncio.run(_run())


def test_process_restart_replays_persisted_terminal():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "restart.json"
            seed = TerminalOutbox(str(outbox_path))
            seed.enqueue(
                {"event_id": "evt-restart", "status": "failed", "msg": "process exited"}
            )

            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            await _wait_until(
                lambda: any(
                    p["cmd"] == "event_result" and p["payload"].get("event_id") == "evt-restart"
                    for p in socket.sent
                )
            )
            request = _latest_result(socket, "evt-restart")
            _respond(
                socket,
                request,
                "send_ack",
                {"event_id": "evt-restart", "status": "failed"},
            )
            await _wait_until(lambda: "evt-restart" not in _read_outbox(outbox_path)["pending"])
            await client.disconnect()

    asyncio.run(_run())


def test_freeze_blocks_provisional_promotion():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "freeze.json"
            seed = TerminalOutbox(str(outbox_path))
            seeded = seed.enqueue(
                {
                    "event_id": "evt-freeze",
                    "status": "failed",
                    "code": "agent_output_unconfirmed",
                    "msg": "guard",
                }
            )
            assert seed.mark_delivery_started(seeded).delivery_started_at > 0

            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            client._terminal._provisional_responded["evt-freeze"] = {
                "event_id": "evt-freeze",
                "status": "responded",
            }
            await _authenticate(client, socket)
            await _wait_until(
                lambda: any(
                    p["cmd"] == "event_result" and p["payload"].get("event_id") == "evt-freeze"
                    for p in socket.sent
                )
            )
            payload = _latest_result(socket, "evt-freeze")["payload"]
            assert payload["status"] == "failed"
            assert payload["code"] == "agent_output_unconfirmed"
            assert "evt-freeze" not in client._terminal._provisional_responded
            await client.disconnect()

    asyncio.run(_run())


def test_stop_canceled_replaces_unsent_output_guard():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "replace-guard.json"
            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            assert client.capture_inbound_terminal_commit_token(
                "evt-stop-replace", "CommitTokenStopReplace"
            )
            client._terminal.terminal_outbox.enqueue(
                {
                    "event_id": "evt-stop-replace",
                    "status": "failed",
                    "code": "agent_output_unconfirmed",
                    "msg": "guard",
                    "terminal_commit_token": "CommitTokenStopReplace",
                }
            )
            unsent = client._terminal.terminal_outbox.get("evt-stop-replace")
            assert unsent is not None
            assert unsent.delivery_started_at is None

            await client.complete_event(
                event_id="evt-stop-replace",
                status="canceled",
                code="event_stopped",
                message="stopped by owner",
            )
            current = client._terminal.terminal_outbox.get("evt-stop-replace")
            assert current.payload["status"] == "canceled"
            assert current.payload["code"] == "event_stopped"
            await client.disconnect()

    asyncio.run(_run())


def test_stop_outbox_discard_on_hard_reject():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "foreign-stop.json"
            stop_path = Path(str(outbox_path) + ".stops")
            socket = MockSocket()

            async def connector(_config):
                return socket

            def on_send(packet):
                if packet["cmd"] == "event_stop_result":
                    socket.push(
                        {
                            "cmd": "error",
                            "seq": packet["seq"],
                            "payload": {"code": 4003, "msg": "foreign event"},
                        }
                    )
                elif packet["cmd"] == "event_result":
                    socket.push(
                        {
                            "cmd": "send_ack",
                            "seq": packet["seq"],
                            "payload": {
                                "event_id": "evt-foreign-stop",
                                "status": "canceled",
                                "terminal_commit_token": "CommitTokenForeignStop",
                                "terminal_committed": True,
                            },
                        }
                    )

            socket.on_send = on_send
            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            assert client.capture_inbound_terminal_commit_token(
                "evt-foreign-stop", "CommitTokenForeignStop"
            )
            await client.complete_event(event_id="evt-foreign-stop", status="canceled")
            await client.complete_stop(
                event_id="evt-foreign-stop",
                stop_id="stop-foreign",
                status="stopped",
            )
            await _wait_until(lambda: StopResultOutbox(str(stop_path)).list_pending() == [])
            await client.disconnect()

    asyncio.run(_run())


def test_first_durable_terminal_wins():
    async def _run():
        with tempfile.TemporaryDirectory() as data_dir:
            outbox_path = Path(data_dir) / "first-wins.json"
            socket = MockSocket()

            async def connector(_config):
                return socket

            client = GrixTransportClient(_make_config(outbox_path), connector=connector)
            await _authenticate(client, socket)
            await client.complete_event(event_id="evt-first", status="canceled")
            await client.complete_event(event_id="evt-first", status="responded")
            pending = client._terminal.terminal_outbox.get("evt-first")
            assert pending.payload["status"] == "canceled"
            await client.disconnect()

    asyncio.run(_run())


if __name__ == "__main__":
    test_suffix_shared_path_isolates_owner()
    test_terminal_outbox_atomic_enqueue_and_ack()
    test_ack_mismatch_retains_pending()
    test_persist_failure_does_not_touch_network()
    test_nack_moves_to_dead_letter()
    test_process_restart_replays_persisted_terminal()
    test_freeze_blocks_provisional_promotion()
    test_stop_canceled_replaces_unsent_output_guard()
    test_stop_outbox_discard_on_hard_reject()
    test_first_durable_terminal_wins()
    print("ok")
