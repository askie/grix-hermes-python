"""主连接重连后补发滞留 event_result 的单元测试。

背景：断连期间 _complete_event_if_needed 先记账（completed_event_ids）再发送，
发送失败时依赖重连后的 _replay_pending_completed_events 补发。补发下游走
_active_client()，而 _active_client() 只认 packet ContextVar —— 重连回调不在
packet handler scope 内，若调用点不显式 set ContextVar，补发会整批放弃，
事件在后端一直悬挂到超时（agent_api_event_result_timeout）。

共享子连接重连路径一直有 set；主连接重连路径此前漏了，本测试锁住它。

走 stub 模式（同 test_agent_deleted.py），不依赖 hermes-agent host。
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


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

from grix_hermes.adapter import _PRIMARY_OWNER_KEY  # noqa: E402
from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.contract import STATUS_RESPONDED  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


class ReplayRecordingClient:
    """记录 complete_event 补发调用的假连接。"""

    instances: list = []

    def __init__(self, config, *, connector=None, on_status=None):
        self.config = config
        self.on_packet = None
        self.on_status = on_status
        self.connected = False
        self.completed: list = []
        ReplayRecordingClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self, reason: str = ""):
        self.connected = False

    async def complete_event(self, *, event_id, status, message=None):
        self.completed.append((event_id, status, message))

    def replay_terminal_outboxes(self):
        return None


def _make_adapter():
    ReplayRecordingClient.instances.clear()
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.connection = GrixConnectionConfig(
        endpoint="ws://x",
        agent_id="100",
        api_key="owner-key",
    )
    inst._client = None
    inst._connector = None
    inst._disconnect_requested = False
    inst._agent_deleted = False
    inst._token_lock_identity = None
    from collections import defaultdict

    inst._owner_states = defaultdict(adapter_mod._OwnerState)
    inst._last_send_at = 0.0
    inst._send_lock = asyncio.Lock()
    inst._reconnect_lock = asyncio.Lock()
    inst._shared_clients = {}
    inst._share_sync_lock = asyncio.Lock()
    inst._shutting_down = False
    inst._background_tasks = set()
    inst._upgrade_checker = None

    inst._set_fatal_error = lambda code, msg, retryable=True: None

    async def _noop():
        pass

    inst._notify_fatal_error = _noop
    inst._safe_release_lock = _noop
    inst._report_skills = _noop
    inst._mark_connected = lambda: None
    inst._mark_disconnected = lambda: None

    # 断线的旧主连接：status 不 connected，逼 _try_reconnect_transport 重建。
    old = ReplayRecordingClient(inst.connection)
    old.connected = False
    inst._client = old
    inst._bind_packet_handler(old)
    return inst


# ── 主连接重连：滞留的 event_result 必须真的补发出去 ──
def test_primary_reconnect_replays_pending_event_results(monkeypatch):
    monkeypatch.setattr(adapter_mod, "GrixTransportClient", ReplayRecordingClient)
    inst = _make_adapter()

    # 断连期间收口失败、只记了账的事件（落在主连接状态桶）。
    state = inst._owner_states[_PRIMARY_OWNER_KEY]
    state.completed_event_ids.add("evt-stranded")
    state.completed_event_results["evt-stranded"] = {
        "status": STATUS_RESPONDED,
        "message": None,
    }

    ok = asyncio.run(inst._try_reconnect_transport(reason="test", max_attempts=2))
    assert ok is True

    # 重连后新建的主连接应当收到补发；修复前 _active_client() 取不到 client，
    # 整批放弃补发，这里会是空列表。
    new_client = inst._client
    assert new_client is not None
    assert new_client.completed == [("evt-stranded", STATUS_RESPONDED, None)]


# ── 补发不得污染 ContextVar：重连返回后必须复原 ──
def test_replay_restores_context_var(monkeypatch):
    monkeypatch.setattr(adapter_mod, "GrixTransportClient", ReplayRecordingClient)
    inst = _make_adapter()

    state = inst._owner_states[_PRIMARY_OWNER_KEY]
    state.completed_event_ids.add("evt-x")
    state.completed_event_results["evt-x"] = {
        "status": STATUS_RESPONDED,
        "message": None,
    }

    async def _run():
        assert adapter_mod._CURRENT_CLIENT_CTX.get() is None
        await inst._try_reconnect_transport(reason="test", max_attempts=2)
        # 重连是管理性调用，跑完必须把 ContextVar 还原成未设置态，
        # 否则会污染后续非 packet 上下文的调用点。
        assert adapter_mod._CURRENT_CLIENT_CTX.get() is None

    asyncio.run(_run())
