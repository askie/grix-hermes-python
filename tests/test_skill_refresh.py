"""skill_refresh local action（对齐 connector skill_refresh）：技能弹窗下拉刷新。"""

from __future__ import annotations

import asyncio
import sys
import types
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── stub host modules（与 test_agent_deleted.py 同模式） ──
def _install_stubs() -> None:
    if "tools" in sys.modules:
        return
    tools_pkg = types.ModuleType("tools")
    reg = types.ModuleType("tools.registry")

    class _Registry:
        def register(self, **kw):
            pass

    reg.registry = _Registry()
    reg.tool_error = lambda msg: f"ERR:{msg}"
    reg.tool_result = lambda obj: f"OK:{obj}"
    tools_pkg.registry = reg
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = reg

    gw = types.ModuleType("gateway")
    gw_cfg = types.ModuleType("gateway.config")

    class _Platform:
        def __init__(self, name):
            self.value = name

    gw_cfg.Platform = _Platform
    gw_cfg.PlatformConfig = lambda **kw: types.SimpleNamespace(**kw)

    gw_session = types.ModuleType("gateway.session")
    gw_session.build_session_key = lambda *a, **kw: "k"

    gw_platforms = types.ModuleType("gateway.platforms")
    gw_platforms_base = types.ModuleType("gateway.platforms.base")
    gw_platforms_base.BasePlatformAdapter = object
    gw_platforms_base.MessageEvent = type("MessageEvent", (), {})
    gw_platforms_base.MessageType = type("MessageType", (), {"TEXT": "text"})
    gw_platforms_base.ProcessingOutcome = type("ProcessingOutcome", (), {})
    gw_platforms_base.SendResult = type("SendResult", (), {})

    gw_run = types.ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: None

    sys.modules["gateway"] = gw
    sys.modules["gateway.config"] = gw_cfg
    sys.modules["gateway.session"] = gw_session
    sys.modules["gateway.platforms"] = gw_platforms
    sys.modules["gateway.platforms.base"] = gw_platforms_base
    sys.modules["gateway.run"] = gw_run


_install_stubs()

from grix_hermes.adapter import GrixAdapter, _CURRENT_CLIENT_CTX, _OwnerState  # noqa: E402
from grix_hermes.contract import (  # noqa: E402
    LOCAL_ACTION_SKILL_REFRESH,
    STABLE_LOCAL_ACTIONS,
    STATUS_FAILED,
    STATUS_OK,
)


def _adapter() -> GrixAdapter:
    adapter = object.__new__(GrixAdapter)
    adapter.name = "grix-test"
    adapter._client = SimpleNamespace(send_local_action_result=AsyncMock())
    adapter._owner_states = defaultdict(_OwnerState)
    adapter._report_skills = AsyncMock()
    return adapter


@contextmanager
def _packet_ctx(adapter):
    token = _CURRENT_CLIENT_CTX.set(adapter._client)
    try:
        yield
    finally:
        _CURRENT_CLIENT_CTX.reset(token)


def test_skill_refresh_declared_in_stable_local_actions():
    # 后端按 auth 声明的 local_actions 门控下发，未声明会被直接拒发。
    assert LOCAL_ACTION_SKILL_REFRESH in STABLE_LOCAL_ACTIONS


def test_skill_refresh_reports_then_acks_ok():
    adapter = _adapter()
    order: list[str] = []

    async def _report(**kwargs):
        assert kwargs.get("force") is True
        assert kwargs.get("raise_on_error") is True
        order.append("report")
        return True

    adapter._report_skills = AsyncMock(side_effect=_report)
    adapter._client.send_local_action_result = AsyncMock(
        side_effect=lambda **kwargs: order.append("ack")
    )

    payload = {
        "action_id": "act-refresh-1",
        "action_type": LOCAL_ACTION_SKILL_REFRESH,
        "params": {"session_id": "sess-1"},
    }
    with _packet_ctx(adapter):
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    # 顺序约束：先重扫上报（agent_skills_update），后回执——后端收到回执即重建快照。
    assert order == ["report", "ack"]
    adapter._client.send_local_action_result.assert_awaited_once_with(
        action_id="act-refresh-1",
        status=STATUS_OK,
        result={"session_id": "sess-1"},
    )


def test_skill_refresh_no_report_acks_failed():
    """未推出上报（扫描异常/空集）时如实回 failed，不谎报成功（对齐 connector）。"""
    adapter = _adapter()
    adapter._report_skills = AsyncMock(return_value=False)

    payload = {
        "action_id": "act-refresh-2",
        "action_type": LOCAL_ACTION_SKILL_REFRESH,
        "params": {"session_id": "sess-1"},
    }
    with _packet_ctx(adapter):
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    adapter._client.send_local_action_result.assert_awaited_once()
    kwargs = adapter._client.send_local_action_result.await_args.kwargs
    assert kwargs["action_id"] == "act-refresh-2"
    assert kwargs["status"] == STATUS_FAILED
    assert kwargs["error_code"] == "SKILL_REFRESH_FAILED"


def test_skill_refresh_scan_error_acks_failed():
    """扫描抛错经 raise_on_error 透传，回 failed 而不是静默 ok。"""
    adapter = _adapter()
    adapter._report_skills = AsyncMock(side_effect=RuntimeError("scan boom"))

    payload = {
        "action_id": "act-refresh-3",
        "action_type": LOCAL_ACTION_SKILL_REFRESH,
        "params": {},
    }
    with _packet_ctx(adapter):
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    adapter._client.send_local_action_result.assert_awaited_once()
    kwargs = adapter._client.send_local_action_result.await_args.kwargs
    assert kwargs["status"] == STATUS_FAILED
    assert kwargs["error_code"] == "SKILL_REFRESH_FAILED"
    assert "scan boom" in kwargs["error_message"]
