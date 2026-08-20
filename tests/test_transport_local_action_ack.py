import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from grix_hermes.contract import CMD_ERROR, CMD_LOCAL_ACTION_ACK, CMD_LOCAL_ACTION_RESULT
from grix_hermes.transport import GrixTransportClient


def _client(capabilities=()):
    client = object.__new__(GrixTransportClient)
    client._negotiated_capabilities = set(capabilities)
    client.send_packet = AsyncMock()
    client.request = AsyncMock()
    return client


def _request_client():
    client = object.__new__(GrixTransportClient)
    client._pending = {}
    client._config = SimpleNamespace(request_timeout_ms=60_000)
    client._ensure_ready = lambda *, require_authed: None
    client._next_seq = lambda: 91
    client._send_packet_internal = AsyncMock()
    return client


def test_confirmed_local_action_result_waits_for_server_ack():
    client = _client({"local_action_result_ack"})
    client.request.return_value = {
        "cmd": CMD_LOCAL_ACTION_ACK,
        "payload": {"action_id": "relay-1", "received": True},
    }

    confirmed = asyncio.run(
        client.send_local_action_result_confirmed(
            action_id="relay-1", status="ok", result={"relay": "enabled"}
        )
    )

    assert confirmed is True
    client.request.assert_awaited_once_with(
        CMD_LOCAL_ACTION_RESULT,
        {"action_id": "relay-1", "status": "ok", "result": {"relay": "enabled"}},
        expected=(CMD_LOCAL_ACTION_ACK, CMD_ERROR),
    )
    client.send_packet.assert_not_awaited()


def test_confirmed_local_action_result_does_not_restart_gate_on_old_server():
    client = _client()

    confirmed = asyncio.run(
        client.send_local_action_result_confirmed(action_id="relay-1", status="ok")
    )

    assert confirmed is False
    client.send_packet.assert_awaited_once_with(
        CMD_LOCAL_ACTION_RESULT, {"action_id": "relay-1", "status": "ok"}
    )


def test_cancelled_request_removes_pending_receipt_waiter():
    async def run():
        client = _request_client()
        task = asyncio.create_task(client.request("local_action_result", {}, expected=("local_action_ack",)))
        await asyncio.sleep(0)
        assert 91 in client._pending
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._pending == {}

    asyncio.run(run())


def test_cancelled_request_removes_pending_while_packet_is_sending():
    async def run():
        client = _request_client()
        started = asyncio.Event()
        unblock = asyncio.Event()

        async def blocked_send(*_args, **_kwargs):
            started.set()
            await unblock.wait()

        client._send_packet_internal = blocked_send
        task = asyncio.create_task(client.request("local_action_result", {}, expected=("local_action_ack",)))
        await started.wait()
        assert 91 in client._pending
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client._pending == {}

    asyncio.run(run())
