"""升级后重启路径单元测试。

修复背景：upgrade_checker 升级成功后原来用裸 os.kill(SIGTERM) 自杀，指望外部
supervisor 复活。Windows 上 SIGTERM = TerminateProcess 硬杀（不 drain、不存
状态），且 supervisor 不可靠时 gateway 会静默躺死。

现在优先走注入的 restart 回调 → GatewayRunner.request_restart（优雅 drain +
自带接班 watcher，不依赖外部 supervisor）；回调不可用才退回 SIGTERM 兜底。

覆盖：
1. 回调成功 → 不再 os.kill；
2. 回调返回 False / 抛异常 / 未注入 → 退回 SIGTERM 兜底；
3. 适配器回调：无 runner 返回 False；有 runner 按 /restart 同款判断
   （systemd/容器 → via_service，否则 detached）调用 request_restart；
4. _start_upgrade_checker 把回调接线进 UpgradeChecker。

走 stub 模式（同 test_final_reply_quote.py），不依赖 hermes-agent host。
"""

import asyncio
import os
import signal
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "tools" not in sys.modules:
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

    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw_cfg = types.ModuleType("gateway.config")

        class _Platform:
            def __init__(self, name):
                self.value = name

            def __eq__(self, other):
                return getattr(other, "value", None) == self.value

            def __hash__(self):
                return hash(self.value)

        gw_cfg.Platform = _Platform
        gw_cfg.PlatformConfig = lambda **kw: SimpleNamespace(**kw)

        gw_session = types.ModuleType("gateway.session")
        gw_session.build_session_key = lambda *a, **kw: "k"

        gw_platforms = types.ModuleType("gateway.platforms")
        gw_platforms_base = types.ModuleType("gateway.platforms.base")
        gw_platforms_base.BasePlatformAdapter = object
        gw_platforms_base.MessageEvent = type("MessageEvent", (), {})
        gw_platforms_base.MessageType = type("MessageType", (), {"TEXT": "text"})
        gw_platforms_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": object()})
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

from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.upgrade_checker import UpgradeChecker  # noqa: E402


def _make_checker(restart=None) -> UpgradeChecker:
    return UpgradeChecker(
        endpoint="wss://example.com/ws",
        api_key="k",
        agent_id="test-agent",
        restart=restart,
    )


# ---------------------------------------------------------------------------
#  UpgradeChecker._restart_process
# ---------------------------------------------------------------------------

def test_restart_callback_success_skips_sigterm(monkeypatch):
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    calls = []
    checker = _make_checker(restart=lambda: calls.append(1) or True)

    checker._restart_process()

    assert calls == [1]
    assert kills == []


def test_restart_callback_false_falls_back_to_sigterm(monkeypatch):
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    checker = _make_checker(restart=lambda: False)

    checker._restart_process()

    assert kills == [(os.getpid(), signal.SIGTERM)]


def test_restart_callback_raises_falls_back_to_sigterm(monkeypatch):
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))

    def _boom():
        raise RuntimeError("no runner")

    checker = _make_checker(restart=_boom)
    checker._restart_process()

    assert kills == [(os.getpid(), signal.SIGTERM)]


def test_no_restart_callback_falls_back_to_sigterm(monkeypatch):
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    checker = _make_checker(restart=None)

    checker._restart_process()

    assert kills == [(os.getpid(), signal.SIGTERM)]


# ---------------------------------------------------------------------------
#  GrixAdapter._request_gateway_restart
# ---------------------------------------------------------------------------

def _adapter_instance() -> "adapter_mod.GrixAdapter":
    return adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)


def test_adapter_restart_no_runner_returns_false(monkeypatch):
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: None)
    assert _adapter_instance()._request_gateway_restart() is False


def test_adapter_restart_detached_by_default(monkeypatch):
    calls = []
    runner = SimpleNamespace(request_restart=lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == [{"detached": True, "via_service": False}]


def test_adapter_restart_via_service_under_systemd(monkeypatch):
    calls = []
    runner = SimpleNamespace(request_restart=lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.setenv("INVOCATION_ID", "abc")

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == [{"detached": False, "via_service": True}]


def test_adapter_restart_via_service_in_container(monkeypatch):
    calls = []
    runner = SimpleNamespace(request_restart=lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: p == "/.dockerenv")

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == [{"detached": False, "via_service": True}]


def test_adapter_restart_already_in_progress_still_true(monkeypatch):
    # request_restart 返回 False 表示重启已在途 —— 对回调而言同样算“已交接”，
    # 绝不能因此退回 SIGTERM。
    runner = SimpleNamespace(request_restart=lambda **kw: False)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: False)

    assert _adapter_instance()._request_gateway_restart() is True


# ---------------------------------------------------------------------------
#  接线：_start_upgrade_checker 注入 restart 回调
# ---------------------------------------------------------------------------

def test_start_upgrade_checker_wires_restart(monkeypatch):
    inst = _adapter_instance()
    inst.name = "grix"
    inst.connection = SimpleNamespace(
        endpoint="wss://example.com/ws", api_key="k", agent_id="test-agent"
    )
    inst._upgrade_checker = None

    async def _noop_start(self):
        return None

    monkeypatch.setattr(UpgradeChecker, "start", _noop_start)
    asyncio.run(inst._start_upgrade_checker())

    checker = inst._upgrade_checker
    assert checker is not None
    assert checker._restart == inst._request_gateway_restart
