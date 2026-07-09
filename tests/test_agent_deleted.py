"""「agent 已在平台删除」fatal 处理单元测试（与 connector 同任务同步）。

走 stub 模式（同 test_agent_share.py），不依赖 hermes-agent host。覆盖：
1. kicked reason=agent_deleted：置 fatal（retryable=False）、断开连接、永久禁止重连
2. kicked 其他 reason：不触发 fatal（维持既有断线重连语义）
3. 内部重连遇 auth_ack 10008：立即终止（单次尝试），置 agent_deleted fatal
4. 内部重连遇一般鉴权拒绝（10001）：立即放弃但不标记 agent_deleted
5. agent_deleted 置位后：内部重连直接短路，不再新建连接
6. connect() 首连遇 10008：置 agent_deleted fatal
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── stub host modules（与 test_agent_share.py 同模式） ──
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

from grix_hermes import adapter as adapter_mod  # noqa: E402
from grix_hermes.contract import AUTH_CODE_AGENT_DELETED  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402
from grix_hermes.transport import GrixAuthRejectedError  # noqa: E402


class FakeTransportClient:
    instances: list = []

    def __init__(self, config, *, connector=None, on_status=None):
        self.config = config
        self.on_packet = None
        self.on_status = on_status
        self.connected = False
        self.disconnect_reason = None
        FakeTransportClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self, reason: str = ""):
        self.connected = False
        self.disconnect_reason = reason


class AuthRejectingClient(FakeTransportClient):
    """connect() 一律抛鉴权拒绝，code 由类属性指定。"""

    reject_code = AUTH_CODE_AGENT_DELETED

    async def connect(self):
        raise GrixAuthRejectedError(self.reject_code, "rejected by test")


def _make_adapter():
    FakeTransportClient.instances.clear()
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

    # framework(hermes host) 侧方法打桩，捕获 fatal 上报
    inst.fatal_calls = []
    inst.notify_count = 0
    inst._set_fatal_error = lambda code, msg, retryable=True: inst.fatal_calls.append(
        (code, msg, retryable)
    )

    async def _notify():
        inst.notify_count += 1

    async def _release_lock():
        pass

    inst._notify_fatal_error = _notify
    inst._safe_release_lock = _release_lock
    inst._mark_connected = lambda: None
    inst._mark_disconnected = lambda: None

    # 主连接（FakeClient 占位，视为已连接）
    inst._client = FakeTransportClient(inst.connection)
    inst._client.connected = True
    inst._bind_packet_handler(inst._client)
    return inst


def _patch_transport(monkeypatch, cls=FakeTransportClient):
    monkeypatch.setattr(adapter_mod, "GrixTransportClient", cls)


# ── 1. kicked reason=agent_deleted → fatal + 断开 + 永久禁止重连 ──
def test_kicked_agent_deleted_marks_fatal_and_disconnects(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    primary = inst._client

    asyncio.run(
        inst._handle_protocol_packet(
            {"cmd": "kicked", "seq": 0, "payload": {"reason": "agent_deleted"}},
            source_client=primary,
        )
    )

    assert inst._agent_deleted is True
    assert inst._disconnect_requested is True  # disconnect() 已执行，禁止一切重连
    assert primary.connected is False
    assert inst.fatal_calls == [
        ("grix_agent_deleted", "agent deleted on platform", False)
    ]
    assert inst.notify_count == 1

    # 置位后内部重连直接短路，不新建连接
    created_before = len(FakeTransportClient.instances)
    assert asyncio.run(inst._try_reconnect_transport(reason="after deleted")) is False
    assert len(FakeTransportClient.instances) == created_before


# ── 2. kicked 其他 reason → 不触发 fatal ──
def test_kicked_other_reason_is_ignored(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    primary = inst._client

    asyncio.run(
        inst._handle_protocol_packet(
            {"cmd": "kicked", "seq": 0, "payload": {"reason": "replaced_by_new_connection"}},
            source_client=primary,
        )
    )

    assert inst._agent_deleted is False
    assert inst._disconnect_requested is False
    assert primary.connected is True
    assert inst.fatal_calls == []


# ── 3. 内部重连遇 auth_ack 10008 → 单次尝试即终止，置 agent_deleted fatal ──
def test_internal_reconnect_aborts_on_agent_deleted_code(monkeypatch):
    _patch_transport(monkeypatch, AuthRejectingClient)
    inst = _make_adapter()
    inst._client.connected = False  # 模拟断线，触发重建

    created_before = len(FakeTransportClient.instances)
    ok = asyncio.run(inst._try_reconnect_transport(reason="test", max_attempts=3))

    assert ok is False
    assert inst._agent_deleted is True
    # 只尝试了一次（不再按 max_attempts 重试）
    assert len(FakeTransportClient.instances) == created_before + 1
    assert inst.fatal_calls and inst.fatal_calls[-1][0] == "grix_agent_deleted"
    assert inst.fatal_calls[-1][2] is False


# ── 4. 内部重连遇一般鉴权拒绝（10001）→ 立即放弃但不标记 agent_deleted ──
def test_internal_reconnect_aborts_on_generic_auth_reject(monkeypatch):
    class Rejecting10001(AuthRejectingClient):
        reject_code = 10001

    _patch_transport(monkeypatch, Rejecting10001)
    inst = _make_adapter()
    inst._client.connected = False

    created_before = len(FakeTransportClient.instances)
    ok = asyncio.run(inst._try_reconnect_transport(reason="test", max_attempts=3))

    assert ok is False
    assert inst._agent_deleted is False
    assert len(FakeTransportClient.instances) == created_before + 1
    assert inst.fatal_calls and inst.fatal_calls[-1][0] == "grix_auth_rejected"
    assert inst.fatal_calls[-1][2] is False


# ── 5. connect() 首连遇 10008 → 置 agent_deleted fatal ──
def test_connect_marks_agent_deleted_on_10008(monkeypatch):
    _patch_transport(monkeypatch, AuthRejectingClient)
    inst = _make_adapter()
    inst._client = None

    ok = asyncio.run(inst.connect())

    assert ok is False
    assert inst._agent_deleted is True
    assert inst.fatal_calls and inst.fatal_calls[-1][0] == "grix_agent_deleted"
    assert inst.fatal_calls[-1][2] is False
