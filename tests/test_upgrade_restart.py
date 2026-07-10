"""升级后重启路径单元测试。

修复背景：upgrade_checker 升级成功后原来用裸 os.kill(SIGTERM) 自杀，指望外部
supervisor 复活。Windows 上 SIGTERM = TerminateProcess 硬杀（不 drain、不存
状态），且 supervisor 不可靠时 gateway 会静默躺死。

现在优先走注入的 restart 回调 → GatewayRunner.request_restart（优雅 drain +
自带接班 watcher，不依赖外部 supervisor）；回调不可用才退回 SIGTERM 兜底。

覆盖：
1. 回调成功 → 不再 os.kill；
2. 回调返回 False / 抛异常 / 未注入 → 退回 SIGTERM 兜底；
3. checker 已停止（网关正在关停）→ 既不回调也不 SIGTERM，
   绝不把运维的计划停机翻转成复活；
4. 适配器回调：无 runner 返回 False；停机排水中返回 True 且不再注入重启；
   request_restart 拒绝且无停机在跑（陈旧闩锁）返回 False 交还 SIGTERM 兜底；
   有 runner 按 /restart 同款判断（systemd/容器 → via_service，否则
   detached）调用 request_restart；
5. is_busy 探针：读 runner 在途 agent 数，异常永不外抛；
6. _start_upgrade_checker 把 is_busy + restart 都接线进 UpgradeChecker。

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


def test_stopped_checker_skips_restart_entirely(monkeypatch):
    # 网关已在关停（checker.stop() 已调用）时，升级完成也不得再注入重启
    # 或 SIGTERM——那会把运维的计划停机翻转成自动复活。
    kills = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)))
    calls = []
    checker = _make_checker(restart=lambda: calls.append(1) or True)
    checker.stop()

    checker._restart_process()

    assert calls == []
    assert kills == []


# ---------------------------------------------------------------------------
#  GrixAdapter._request_gateway_restart
# ---------------------------------------------------------------------------

def _adapter_instance() -> "adapter_mod.GrixAdapter":
    return adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)


def _patch_container_probes(monkeypatch, present=()):
    # 只劫持两个容器探测路径，其余路径委托真实 os.path.exists，
    # 避免全局替换掩盖被测代码将来新增的真实文件检查。
    real_exists = os.path.exists
    probes = {"/.dockerenv", "/run/.containerenv"}
    monkeypatch.setattr(
        os.path,
        "exists",
        lambda p: (p in present) if p in probes else real_exists(p),
    )


def _make_runner(calls, result=True, stop_task=None):
    return SimpleNamespace(
        request_restart=lambda **kw: calls.append(kw) or result,
        _stop_task=stop_task,
    )


def test_adapter_restart_no_runner_returns_false(monkeypatch):
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: None)
    assert _adapter_instance()._request_gateway_restart() is False


def test_adapter_restart_detached_by_default(monkeypatch):
    calls = []
    runner = _make_runner(calls)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_container_probes(monkeypatch)

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == [{"detached": True, "via_service": False}]


def test_adapter_restart_via_service_under_systemd(monkeypatch):
    calls = []
    runner = _make_runner(calls)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.setenv("INVOCATION_ID", "abc")

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == [{"detached": False, "via_service": True}]


def test_adapter_restart_via_service_in_container(monkeypatch):
    calls = []
    runner = _make_runner(calls)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_container_probes(monkeypatch, present={"/.dockerenv"})

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == [{"detached": False, "via_service": True}]


def test_adapter_restart_active_stop_not_hijacked(monkeypatch):
    # 网关停机/重启已在排水中（_stop_task 未完成）：进程生命周期已有归属，
    # 不得再调 request_restart（会把计划停机的标志翻成重启），返回 True
    # 让上层也不要 SIGTERM。
    calls = []
    draining = SimpleNamespace(done=lambda: False)
    runner = _make_runner(calls, stop_task=draining)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_container_probes(monkeypatch)

    assert _adapter_instance()._request_gateway_restart() is True
    assert calls == []


def test_adapter_restart_stale_latch_returns_false(monkeypatch):
    # request_restart 返回 False 且没有任何停机在跑 = 陈旧的一次性重启闩锁
    # （此前某次重启的 stop 任务夭折）。必须返回 False 让上层退回 SIGTERM，
    # 否则升级已上报 installed 却永远等不来重启，agent 静默停在旧版。
    calls = []
    runner = _make_runner(calls, result=False, stop_task=None)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    monkeypatch.delenv("INVOCATION_ID", raising=False)
    _patch_container_probes(monkeypatch)

    assert _adapter_instance()._request_gateway_restart() is False
    assert calls == [{"detached": True, "via_service": False}]


# ---------------------------------------------------------------------------
#  GrixAdapter._gateway_is_busy
# ---------------------------------------------------------------------------

def test_gateway_is_busy_reads_running_agent_count(monkeypatch):
    runner = SimpleNamespace(_running_agent_count=lambda: 2)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner)
    assert _adapter_instance()._gateway_is_busy() is True

    runner_idle = SimpleNamespace(_running_agent_count=lambda: 0)
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: runner_idle)
    assert _adapter_instance()._gateway_is_busy() is False


def test_gateway_is_busy_never_raises(monkeypatch):
    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", lambda: None)
    assert _adapter_instance()._gateway_is_busy() is False

    def _boom():
        raise RuntimeError("runner gone")

    monkeypatch.setattr(sys.modules["gateway.run"], "_gateway_runner_ref", _boom)
    assert _adapter_instance()._gateway_is_busy() is False


# ---------------------------------------------------------------------------
#  接线：_start_upgrade_checker 注入 is_busy + restart 回调
# ---------------------------------------------------------------------------

def test_start_upgrade_checker_wires_callbacks(monkeypatch):
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
    assert checker._is_busy == inst._gateway_is_busy


# ---------------------------------------------------------------------------
#  UpgradeChecker._wait_until_idle（任务不清空不重启，无等待上限）
# ---------------------------------------------------------------------------

def _make_checker_with_busy(is_busy) -> UpgradeChecker:
    return UpgradeChecker(
        endpoint="wss://example.com/ws",
        api_key="k",
        agent_id="test-agent",
        is_busy=is_busy,
    )


def test_wait_until_idle_waits_past_old_1h_cap(monkeypatch):
    # 忙碌 800 轮（按真实 5s/轮折算超过旧的 3600s 上限）：旧逻辑会在 1 小时处
    # 强制放行；现在必须等到不忙才返回 True。sleep 替换为瞬时完成。
    calls = {"n": 0}

    def is_busy():
        calls["n"] += 1
        return calls["n"] <= 800

    async def fast_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    checker = _make_checker_with_busy(is_busy)

    assert asyncio.run(checker._wait_until_idle()) is True
    assert calls["n"] > 800  # 一直等到 is_busy 变 False 才放行


def test_wait_until_idle_stopped_mid_wait_returns_false(monkeypatch):
    checker = _make_checker_with_busy(lambda: True)
    rounds = {"n": 0}

    async def stopping_sleep(_seconds):
        rounds["n"] += 1
        if rounds["n"] >= 3:
            checker._stopped = True

    monkeypatch.setattr(asyncio, "sleep", stopping_sleep)

    assert asyncio.run(checker._wait_until_idle()) is False


def test_wait_until_idle_immediate_when_not_busy():
    checker = _make_checker_with_busy(lambda: False)
    assert asyncio.run(checker._wait_until_idle()) is True


def test_wait_until_idle_immediate_when_no_probe():
    checker = _make_checker_with_busy(None)
    assert asyncio.run(checker._wait_until_idle()) is True
