"""升级成功后插件被静默 disable 的防护测试。

事故背景：``hermes plugins update`` 报告成功，但重启后 config.yaml 里
grix-hermes 落在 ``disabled`` 而不在 ``enabled`` ——网关日志只打印一行
"No messaging platforms enabled."，没有任何报错，五个 profile 静默断连
Grix 约一小时，升级回执系统仍然上报 success/installed。

修复点：``_do_upgrade`` 在 ``hermes plugins update``/``hermes plugins
install`` 成功后，显式调用 ``_ensure_enabled``——重新 enable 并用
``hermes plugins show`` 校验状态；校验不过则抛异常，中止 ``_do_upgrade``，
使 ``_check`` 的 except 分支上报 failed 并跳过重启，旧的、仍连着 Grix 的
进程继续运行，而不是重启进一个断连状态。

走 stub 模式（同 test_upgrade_restart.py），不依赖 hermes-agent host。
"""

import asyncio
import sys
import types
from pathlib import Path

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
        gw_cfg.PlatformConfig = lambda **kw: types.SimpleNamespace(**kw)

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

from grix_hermes.upgrade_checker import UpgradeChecker  # noqa: E402


def _make_checker() -> UpgradeChecker:
    return UpgradeChecker(endpoint="wss://example.com/ws", api_key="k", agent_id="test-agent")


def _fake_run_cmd(responses):
    calls = []

    async def _run(cmd, timeout=120):
        calls.append(list(cmd))
        for prefix, result in responses:
            if cmd[: len(prefix)] == prefix:
                return result
        raise AssertionError(f"unexpected command: {cmd}")

    return calls, _run


def test_update_success_and_enabled_returns_cleanly(monkeypatch):
    calls, fake = _fake_run_cmd([
        (["hermes", "plugins", "update"], (0, "", "")),
        (["hermes", "plugins", "enable"], (0, "", "")),
        (["hermes", "plugins", "show"], (0, "grix-hermes v1.13.9\nStatus: enabled\n", "")),
    ])
    monkeypatch.setattr(UpgradeChecker, "_run_cmd", staticmethod(fake))
    checker = _make_checker()

    asyncio.run(checker._do_upgrade())

    assert calls[0][:3] == ["hermes", "plugins", "update"]
    assert calls[1][:3] == ["hermes", "plugins", "enable"]
    assert calls[2][:3] == ["hermes", "plugins", "show"]


def test_update_success_but_left_disabled_raises_and_skips_restart(monkeypatch):
    # 复现事故：update 退出码 0，但插件实际落在 disabled。
    calls, fake = _fake_run_cmd([
        (["hermes", "plugins", "update"], (0, "", "")),
        (["hermes", "plugins", "enable"], (0, "", "")),
        (["hermes", "plugins", "show"], (0, "grix-hermes v1.13.9\nStatus: disabled\n", "")),
    ])
    monkeypatch.setattr(UpgradeChecker, "_run_cmd", staticmethod(fake))
    checker = _make_checker()

    try:
        asyncio.run(checker._do_upgrade())
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "not enabled" in str(exc)


def test_install_fallback_success_also_verified(monkeypatch):
    calls, fake = _fake_run_cmd([
        (["hermes", "plugins", "update"], (1, "", "some transient error")),
        (["hermes", "plugins", "install"], (0, "", "")),
        (["hermes", "plugins", "enable"], (0, "", "")),
        (["hermes", "plugins", "show"], (0, "Status: enabled\n", "")),
    ])
    monkeypatch.setattr(UpgradeChecker, "_run_cmd", staticmethod(fake))
    # UPDATE_MAX_ATTEMPTS retries would slow the test down with real sleeps;
    # shrink the backoff window for this run only.
    monkeypatch.setattr("grix_hermes.upgrade_checker.UPDATE_RETRY_BASE_S", 0.0)
    checker = _make_checker()

    asyncio.run(checker._do_upgrade())

    assert any(c[:3] == ["hermes", "plugins", "install"] for c in calls)
    assert calls[-2][:3] == ["hermes", "plugins", "enable"]
    assert calls[-1][:3] == ["hermes", "plugins", "show"]
