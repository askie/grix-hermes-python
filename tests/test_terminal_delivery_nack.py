"""TerminalDeliveryController NACK 处理单元测试。

验证：服务端对 event_result 返回 4xxx 永久拒绝（如 4001 unsupported event_result status）
时，不触发 transport reconnect，避免冲掉正在发送的普通消息、导致框架重试产生重复气泡。
"""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw.config = types.ModuleType("gateway.config")
        gw.config.Platform = lambda name: SimpleNamespace(value=name)
        sys.modules["gateway"] = gw
        sys.modules["gateway.config"] = gw.config


_install_stubs()

from grix_hermes.contract import (
    CAP_TERMINAL_COMMIT_V1,
    CMD_ERROR,
    CMD_SEND_NACK,
)
from grix_hermes.terminal_delivery import TerminalDeliveryController
from grix_hermes.persistence.terminal_outbox import TerminalOutbox


class FakeClient:
    """最小 transport 假件，只暴露 terminal_delivery 需要的属性。"""

    def __init__(self, tmp_path):
        self._config = SimpleNamespace(
            terminal_outbox_path=str(tmp_path / "outbox.json"),
            terminal_commit_token_store_path=str(tmp_path / "tokens.json"),
            stop_result_outbox_path=str(tmp_path / "stop.json"),
            terminal_committed_store_path=str(tmp_path / "committed.json"),
        )
        self.negotiated_capabilities = {CAP_TERMINAL_COMMIT_V1}
        self.ack_policy = {"max_retries": 1, "push_ack_timeout_ms": 100}
        self._generation = 1
        self.request_calls = []
        self.reconnect_reasons = []
        self._nack_payload = None

    def is_connection_current(self, generation: int) -> bool:
        return generation == self._generation

    def is_ready_for_outbound(self) -> bool:
        return True

    async def request(self, cmd, payload, *, expected, timeout_ms=None):
        self.request_calls.append((cmd, payload, expected, timeout_ms))
        if self._nack_payload is not None:
            return {"cmd": CMD_SEND_NACK, "payload": self._nack_payload}
        raise RuntimeError("unexpected request")

    async def reconnect_after_outbound_failure(self, reason: str) -> None:
        self.reconnect_reasons.append(reason)


def _make_controller(tmp_path):
    client = FakeClient(tmp_path)
    return TerminalDeliveryController(client), client


def test_permanent_nack_moves_to_dead_letter_without_reconnect(tmp_path):
    """4xxx 永久 NACK 应进死信，不应 reconnect。"""
    ctrl, client = _make_controller(tmp_path)
    payload = {
        "event_id": "ev-1",
        "status": "responded",
        "terminal_commit_token": "tok-abc",
    }
    entry = ctrl.terminal_outbox.enqueue(payload)
    client._nack_payload = {"code": 4001, "msg": "unsupported event_result status"}

    delay = asyncio.run(ctrl._send_event_result_reliable(entry, generation=1))

    assert delay == 0
    assert client.reconnect_reasons == []
    assert ctrl.terminal_outbox.get("ev-1") is None
    dead = ctrl.terminal_outbox.list_dead_letters()
    assert len(dead) == 1
    assert dead[0].code == 4001
    assert dead[0].message == "unsupported event_result status"


def test_transient_nack_with_token_still_reconnects(tmp_path):
    """非 4xxx 的 tokenized 拒绝（如 5001）仍应 reconnect 以便重试。"""
    ctrl, client = _make_controller(tmp_path)
    payload = {
        "event_id": "ev-2",
        "status": "responded",
        "terminal_commit_token": "tok-xyz",
    }
    entry = ctrl.terminal_outbox.enqueue(payload)
    client._nack_payload = {"code": 5001, "msg": "server busy"}

    delay = asyncio.run(ctrl._send_event_result_reliable(entry, generation=1))

    assert delay == 15_000
    assert client.reconnect_reasons == ["tokenized terminal rejected"]


def test_rate_limit_nack_4008_still_reconnects(tmp_path):
    """4008 握手限流仍应 reconnect。"""
    ctrl, client = _make_controller(tmp_path)
    payload = {
        "event_id": "ev-3",
        "status": "responded",
        "terminal_commit_token": "tok-rate",
    }
    entry = ctrl.terminal_outbox.enqueue(payload)
    client._nack_payload = {"code": 4008, "msg": "rate limited"}

    delay = asyncio.run(ctrl._send_event_result_reliable(entry, generation=1))

    assert delay == 15_000
    assert client.reconnect_reasons == ["tokenized terminal rejected"]
