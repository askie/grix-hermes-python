"""Transport websocket write serialization tests."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List

from grix_hermes.protocol import GrixConnectionConfig
from grix_hermes.transport import GrixTransportClient


class SlowSocket:
    def __init__(self):
        self.active_writes = 0
        self.max_active_writes = 0
        self.sent: List[Dict[str, Any]] = []

    async def send_text(self, text: str) -> None:
        self.active_writes += 1
        self.max_active_writes = max(self.max_active_writes, self.active_writes)
        await asyncio.sleep(0.01)
        self.sent.append(json.loads(text))
        self.active_writes -= 1

    async def receive(self) -> Dict[str, Any]:
        await asyncio.Event().wait()
        return {"kind": "closed", "reason": "test"}

    async def close(self, reason: str = "") -> None:
        return None


def test_transport_serializes_concurrent_websocket_writes():
    async def run():
        socket = SlowSocket()
        client = GrixTransportClient(
            GrixConnectionConfig(
                endpoint="wss://example.invalid",
                agent_id="agent-1",
                api_key="secret",
            )
        )
        client._socket = socket
        client._status.update({"connected": True, "authed": True})

        await asyncio.gather(
            client.send_packet("one", {"v": 1}),
            client.send_packet("two", {"v": 2}),
            client.send_packet("three", {"v": 3}),
        )

        assert socket.max_active_writes == 1
        assert [packet["cmd"] for packet in socket.sent] == ["one", "two", "three"]

    asyncio.run(run())
