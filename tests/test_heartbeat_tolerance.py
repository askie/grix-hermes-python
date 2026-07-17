"""心跳两次容忍判死语义的单元测试。

背景（2026-07-17 kimi WS 抖动事故，与 grix-connector 同步的共享语义）：
单次 pong 超时可能只是链路拥塞（如重连补发洪峰把心跳挤在队列后面），
一票判死会形成断连-补发恶性循环。语义：连续两次失败才 disconnect，
成功一次即清零计数。
"""

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.transport import GrixTransportClient  # noqa: E402


def _make_client() -> GrixTransportClient:
    client = GrixTransportClient.__new__(GrixTransportClient)
    client._disconnect_requested = False
    return client


_real_sleep = asyncio.sleep


async def _fast_sleep(_seconds: float) -> None:
    await _real_sleep(0)


def test_single_failure_tolerated() -> None:
    async def run() -> None:
        client = _make_client()
        calls = {"n": 0}

        async def request(cmd, payload, *, expected, timeout_ms):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("pong timeout")
            if calls["n"] >= 4:
                raise asyncio.CancelledError()
            return {}

        client.request = request
        client.disconnect = AsyncMock()

        with patch("asyncio.sleep", _fast_sleep):
            await client._heartbeat_loop(5)

        # 第 1 次失败被容忍，第 2、3 次成功，循环由 CancelledError 正常退出
        client.disconnect.assert_not_awaited()

    asyncio.run(run())


def test_two_consecutive_failures_disconnect() -> None:
    async def run() -> None:
        client = _make_client()

        async def request(cmd, payload, *, expected, timeout_ms):
            raise RuntimeError("pong timeout")

        client.request = request
        client.disconnect = AsyncMock()

        with patch("asyncio.sleep", _fast_sleep):
            await client._heartbeat_loop(5)

        client.disconnect.assert_awaited_once()
        assert "heartbeat failed" in client.disconnect.await_args.args[0]

    asyncio.run(run())


def test_success_resets_failure_count() -> None:
    async def run() -> None:
        client = _make_client()
        calls = {"n": 0}

        # 失败-成功交替：永远到不了连续两次失败
        async def request(cmd, payload, *, expected, timeout_ms):
            calls["n"] += 1
            if calls["n"] >= 7:
                raise asyncio.CancelledError()
            if calls["n"] % 2 == 1:
                raise RuntimeError("pong timeout")
            return {}

        client.request = request
        client.disconnect = AsyncMock()

        with patch("asyncio.sleep", _fast_sleep):
            await client._heartbeat_loop(5)

        client.disconnect.assert_not_awaited()

    asyncio.run(run())


def test_disconnect_requested_suppresses() -> None:
    async def run() -> None:
        client = _make_client()
        client._disconnect_requested = True

        async def request(cmd, payload, *, expected, timeout_ms):
            raise RuntimeError("pong timeout")

        client.request = request
        client.disconnect = AsyncMock()

        with patch("asyncio.sleep", _fast_sleep):
            await client._heartbeat_loop(5)

        client.disconnect.assert_not_awaited()

    asyncio.run(run())
