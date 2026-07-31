"""「agent 已在平台删除」fatal 处理单元测试（与 connector 同任务同步）。

走 stub 模式（同 test_agent_share.py），不依赖 hermes-agent host。覆盖：
1. kicked reason=agent_deleted：置 fatal（retryable=False）、断开连接、永久禁止重连
2. kicked 其他 reason：不触发 fatal（维持既有断线重连语义）
3. 内部重连遇 auth_ack 10008：立即终止（单次尝试），置 agent_deleted fatal
4. 内部重连遇一般鉴权拒绝（10001）：按退避策略重试，不标记永久 fatal
5. agent_deleted 置位后：内部重连直接短路，不再新建连接
6. connect() 首连遇 10008：置 agent_deleted fatal
7. connect() 首连遇一般鉴权拒绝（10001）：交给 gateway watcher 持续重试
8. 重连退避指数增长并封顶，避免忙循环
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
    inst._status_reconnect_lock = asyncio.Lock()
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


# ── 4. 内部重连遇一般鉴权拒绝（10001）→ 受控重试且保持 retryable ──
def test_internal_reconnect_retries_generic_auth_reject(monkeypatch):
    class Rejecting10001(AuthRejectingClient):
        reject_code = 10001

    _patch_transport(monkeypatch, Rejecting10001)
    monkeypatch.setattr(adapter_mod.random, "uniform", lambda _low, _high: 0.0)
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    inst = _make_adapter()
    inst._client.connected = False

    created_before = len(FakeTransportClient.instances)
    ok = asyncio.run(inst._try_reconnect_transport(reason="test", max_attempts=3))

    assert ok is False
    assert inst._agent_deleted is False
    assert len(FakeTransportClient.instances) == created_before + 3
    assert delays == [2.0, 4.0]
    assert inst.fatal_calls == []


def test_internal_reconnect_recovers_after_transient_auth_reject(monkeypatch):
    class Recovering10001(FakeTransportClient):
        connect_attempts = 0

        async def connect(self):
            type(self).connect_attempts += 1
            if type(self).connect_attempts < 3:
                raise GrixAuthRejectedError(10001, "service recovering")
            self.connected = True

    _patch_transport(monkeypatch, Recovering10001)
    monkeypatch.setattr(adapter_mod.random, "uniform", lambda _low, _high: 0.0)
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    async def noop():
        pass

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    inst = _make_adapter()
    inst._client.connected = False
    inst._report_skills = noop
    inst._replay_pending_completed_events = noop
    inst._push_all_queue_snapshots = noop

    ok = asyncio.run(inst._try_reconnect_transport(reason="test", max_attempts=3))

    assert ok is True
    assert Recovering10001.connect_attempts == 3
    assert delays == [2.0, 4.0]
    assert inst.fatal_calls == []


def test_reconnect_backoff_is_exponential_and_capped(monkeypatch):
    monkeypatch.setattr(adapter_mod.random, "uniform", lambda _low, _high: 0.0)

    delays = [
        adapter_mod._reconnect_delay_seconds(attempt)
        for attempt in range(1, 8)
    ]

    assert delays == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]
    assert [
        adapter_mod._background_reconnect_delay_seconds(attempt)
        for attempt in range(1, 7)
    ] == [30.0, 60.0, 120.0, 240.0, 300.0, 300.0]

    monkeypatch.setattr(adapter_mod.random, "uniform", lambda _low, _high: 0.2)
    assert adapter_mod._background_reconnect_delay_seconds(100_000) == 300.0


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


# ── 7. connect() 首连遇 10001 → 保留后台重连资格 ──
def test_connect_keeps_generic_auth_reject_retryable(monkeypatch):
    class Rejecting10001(AuthRejectingClient):
        reject_code = 10001

    _patch_transport(monkeypatch, Rejecting10001)
    inst = _make_adapter()
    inst._client = None

    ok = asyncio.run(inst.connect())

    assert ok is False
    assert inst._agent_deleted is False
    assert inst.fatal_calls[-1][0] == "grix_auth_rejected"
    assert inst.fatal_calls[-1][2] is True


def test_stale_primary_status_callback_is_ignored(monkeypatch):
    inst = _make_adapter()
    inst.is_connected = True
    stale_client = FakeTransportClient(inst.connection)
    reconnect_calls = 0

    async def fake_reconnect(*, reason):
        nonlocal reconnect_calls
        reconnect_calls += 1
        return False

    monkeypatch.setattr(inst, "_try_reconnect_transport", fake_reconnect)

    asyncio.run(
        inst._handle_transport_status(
            {"connected": False, "last_error": "stale"},
            source_client=stale_client,
        )
    )

    assert reconnect_calls == 0
    assert inst.fatal_calls == []
    assert inst.notify_count == 0


def test_duplicate_primary_status_callbacks_are_coalesced(monkeypatch):
    inst = _make_adapter()
    inst.is_connected = True
    source_client = inst._client
    reconnect_calls = 0

    async def fake_reconnect(*, reason):
        nonlocal reconnect_calls
        reconnect_calls += 1
        # Match the real failed rebuild: it detaches the old source client.
        inst._client = None
        await asyncio.sleep(0)
        return False

    monkeypatch.setattr(inst, "_try_reconnect_transport", fake_reconnect)

    async def run_duplicate_callbacks():
        status = {"connected": False, "last_error": "closed"}
        await asyncio.gather(
            inst._handle_transport_status(status, source_client=source_client),
            inst._handle_transport_status(status, source_client=source_client),
        )

    asyncio.run(run_duplicate_callbacks())

    assert reconnect_calls == 1
    assert inst.notify_count == 1
    assert inst.fatal_calls == [
        ("grix_connection_lost", "closed", True),
    ]


def test_internal_reconnect_does_not_resurrect_after_shutdown(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    inst._client.connected = False
    inst._disconnect_requested = True
    created_before = len(FakeTransportClient.instances)

    ok = asyncio.run(inst._try_reconnect_transport(reason="shutdown"))

    assert ok is False
    assert len(FakeTransportClient.instances) == created_before


def test_waiting_internal_reconnect_rechecks_shutdown_after_lock(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    inst._client.connected = False
    created_before = len(FakeTransportClient.instances)

    async def run_race():
        await inst._reconnect_lock.acquire()
        reconnect_task = asyncio.create_task(
            inst._try_reconnect_transport(reason="queued before shutdown")
        )
        await asyncio.sleep(0)
        inst._disconnect_requested = True
        inst._shutting_down = True
        inst._reconnect_lock.release()
        return await reconnect_task

    ok = asyncio.run(run_race())

    assert ok is False
    assert len(FakeTransportClient.instances) == created_before


def test_failed_candidate_status_does_not_amplify_reconnect(monkeypatch):
    status_tasks = []

    class StatusEmittingRejectClient(FakeTransportClient):
        async def connect(self):
            if self.on_status is not None:
                result = self.on_status(
                    {"connected": False, "last_error": "auth failed"}
                )
                if asyncio.iscoroutine(result):
                    status_tasks.append(asyncio.create_task(result))
            raise GrixAuthRejectedError(10001, "service recovering")

    _patch_transport(monkeypatch, StatusEmittingRejectClient)
    monkeypatch.setattr(adapter_mod.random, "uniform", lambda _low, _high: 0.0)
    real_sleep = asyncio.sleep

    async def fake_sleep(_delay):
        await real_sleep(0)

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    inst = _make_adapter()
    inst.is_connected = True
    inst._client.connected = False
    created_before = len(FakeTransportClient.instances)

    async def run_failure():
        ok = await inst._try_reconnect_transport(reason="closed", max_attempts=2)
        if status_tasks:
            await asyncio.gather(*status_tasks)
        # Give callbacks a chance to enqueue further work if stale-source
        # filtering is broken.
        await asyncio.sleep(0)
        return ok

    ok = asyncio.run(run_failure())

    assert ok is False
    assert len(FakeTransportClient.instances) == created_before + 2
    assert inst.notify_count == 0
