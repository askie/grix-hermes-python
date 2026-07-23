"""升级检查器「插件目录被遮蔽」防护与启动版本不符收敛测试。

事故背景：plugins/ 下残留的备份目录（如 grix-hermes.bak.v1.8.9/，含自己的
plugin.yaml）会被 Hermes 加载器发现并按排序覆盖正规 grix-hermes/ —— gateway
实际跑的是备份旧代码。升级检查器据此判断「有新版本」→ plugins update 更新
的却是正规目录（新代码永远不会被加载）→ 重启后版本仍不符 → 再升级 → 无限
重启循环；且成功路径从不记失败，限流（MAX_VERSION_ATTEMPTS / MAX_DAILY_ATTEMPTS）
完全失效。

修复点：
1. ``_check`` 检测到运行目录名 != PLUGIN_NAME（被遮蔽）时拒绝升级，直接返回；
2. ``_handle_pending_on_startup`` 版本不符一律 ``_record_failure(target)``，
   让既有版本/日限流收敛重试；
3. 不符且被遮蔽时上报 failed/PLUGIN_SHADOWED（区别于真回滚 rolled_back），
   日志指明须人工删除备份目录。

走 stub 模式（同 test_upgrade_restart.py），不依赖 hermes-agent host。
"""

import asyncio
import json
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

from grix_hermes import upgrade_checker as uc  # noqa: E402

AGENT_ID = "test-agent"


def _make_checker() -> uc.UpgradeChecker:
    return uc.UpgradeChecker(
        endpoint="wss://example.com/ws",
        api_key="k",
        agent_id=AGENT_ID,
    )


def _isolate_home(monkeypatch, tmp_path):
    """升级状态/挂起文件落到临时 HOME，互不污染。"""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


def _capture_reports(monkeypatch):
    reports = []

    async def _fake_report(self, report):
        reports.append(report)

    monkeypatch.setattr(uc.UpgradeChecker, "_report", _fake_report)
    return reports


def _write_pending(target="1.9.0", from_version="1.8.9"):
    uc._write_pending(from_version, target, AGENT_ID)


# ---------------------------------------------------------------------------
#  1. _check 被遮蔽时拒绝升级
# ---------------------------------------------------------------------------

def test_check_refuses_upgrade_when_shadowed(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_is_shadowed_copy", lambda: True)
    monkeypatch.setattr(uc, "_running_plugin_dir_name", lambda: "grix-hermes.bak.v1.8.9")

    queried = []

    async def _fake_query(self):
        queried.append(1)
        return {"available": True, "release": {"version": "1.9.0"}}

    monkeypatch.setattr(uc.UpgradeChecker, "_query_upgrade", _fake_query)
    checker = _make_checker()

    asyncio.run(checker._check())

    assert queried == []  # 连版本查询都不该发，直接放弃


def test_check_proceeds_when_not_shadowed(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_is_shadowed_copy", lambda: False)

    async def _fake_query(self):
        return {"available": False}

    monkeypatch.setattr(uc.UpgradeChecker, "_query_upgrade", _fake_query)
    checker = _make_checker()

    asyncio.run(checker._check())  # 不抛异常即通过（走到了查询）


# ---------------------------------------------------------------------------
#  2. 启动版本不符：计入失败限流
# ---------------------------------------------------------------------------

def test_startup_mismatch_records_failure(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_is_shadowed_copy", lambda: False)
    monkeypatch.setattr(uc, "resolve_plugin_version", lambda: "1.8.9")
    reports = _capture_reports(monkeypatch)
    _write_pending(target="1.9.0")

    checker = _make_checker()
    asyncio.run(checker._handle_pending_on_startup())

    state = uc._read_state(AGENT_ID)
    assert state["version_attempts"].get("1.9.0") == 1
    assert sum(state["daily_attempts"].values()) == 1
    assert reports and reports[0]["status"] == "rolled_back"
    assert reports[0]["error_code"] == "STARTUP_MISMATCH"
    assert not uc._pending_exists(AGENT_ID)


def test_startup_match_does_not_record_failure(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_is_shadowed_copy", lambda: False)
    monkeypatch.setattr(uc, "resolve_plugin_version", lambda: "1.9.0")
    reports = _capture_reports(monkeypatch)
    _write_pending(target="1.9.0")

    checker = _make_checker()
    asyncio.run(checker._handle_pending_on_startup())

    state = uc._read_state(AGENT_ID)
    assert state["version_attempts"] == {}
    assert state["daily_attempts"] == {}
    assert reports and reports[0]["status"] == "success"


# ---------------------------------------------------------------------------
#  3. 启动版本不符 + 被遮蔽：上报 PLUGIN_SHADOWED 并计入失败
# ---------------------------------------------------------------------------

def test_startup_mismatch_shadowed_reports_plugin_shadowed(monkeypatch, tmp_path):
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_is_shadowed_copy", lambda: True)
    monkeypatch.setattr(uc, "_running_plugin_dir_name", lambda: "grix-hermes.bak.v1.8.9")
    monkeypatch.setattr(uc, "resolve_plugin_version", lambda: "1.8.9")
    reports = _capture_reports(monkeypatch)
    _write_pending(target="1.9.0")

    checker = _make_checker()
    asyncio.run(checker._handle_pending_on_startup())

    state = uc._read_state(AGENT_ID)
    assert state["version_attempts"].get("1.9.0") == 1
    assert reports and reports[0]["status"] == "failed"
    assert reports[0]["error_code"] == "PLUGIN_SHADOWED"
    assert "grix-hermes.bak.v1.8.9" in reports[0]["error_msg"]
    assert not uc._pending_exists(AGENT_ID)


def test_startup_mismatch_converges_after_max_attempts(monkeypatch, tmp_path):
    """连续 mismatch 记失败达到 MAX_VERSION_ATTEMPTS 后，_check 限流不再升级。"""
    _isolate_home(monkeypatch, tmp_path)
    monkeypatch.setattr(uc, "_is_shadowed_copy", lambda: False)
    monkeypatch.setattr(uc, "resolve_plugin_version", lambda: "1.8.9")
    _capture_reports(monkeypatch)

    checker = _make_checker()
    for _ in range(uc.MAX_VERSION_ATTEMPTS):
        _write_pending(target="1.9.0")
        asyncio.run(checker._handle_pending_on_startup())

    assert checker._check_rate_limit("1.9.0") is False
