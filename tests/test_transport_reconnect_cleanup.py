"""Transport cleanup tests for reconnect failure paths."""

import asyncio
import re

import pytest

from grix_hermes.protocol import GrixConnectionConfig
from grix_hermes.transport import GrixAuthRejectedError, GrixTransportClient


class _FakeSocket:
    def __init__(self):
        self.close_reasons = []

    async def close(self, reason=""):
        self.close_reasons.append(reason)


@pytest.mark.parametrize(
    "auth_error",
    [
        GrixAuthRejectedError(10001, "service recovering"),
        RuntimeError("authentication backend unavailable"),
    ],
)
def test_connect_auth_failure_closes_socket_and_reader(auth_error):
    async def run():
        socket = _FakeSocket()
        reader_started = asyncio.Event()
        reader_cancelled = asyncio.Event()
        statuses = []

        async def connector(_config):
            return socket

        async def on_status(status):
            statuses.append(status)

        client = GrixTransportClient(
            GrixConnectionConfig(
                endpoint="wss://example.invalid",
                agent_id="agent-1",
                api_key="secret",
            ),
            connector=connector,
            on_status=on_status,
        )

        async def reader_loop():
            reader_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                reader_cancelled.set()
                raise

        async def authenticate():
            await reader_started.wait()
            raise auth_error

        client._reader_loop = reader_loop
        client.authenticate = authenticate

        with pytest.raises(type(auth_error), match=re.escape(str(auth_error))):
            await client.connect()

        # on_status callbacks are scheduled tasks; let the final disconnected
        # notification drain before asserting the converged state.
        await asyncio.sleep(0)
        pending_status_tasks = list(client._status_tasks)
        if pending_status_tasks:
            await asyncio.gather(*pending_status_tasks)
        await asyncio.sleep(0)

        assert socket.close_reasons == ["auth failed"]
        assert reader_cancelled.is_set()
        assert client._socket is None
        assert client._reader_task is None
        assert client._heartbeat_task is None
        assert client.status["running"] is False
        assert client.status["connected"] is False
        assert client.status["authed"] is False
        assert client.status["last_error"] == "auth failed"
        assert statuses[-1]["connected"] is False
        assert statuses[-1]["running"] is False

    asyncio.run(run())
