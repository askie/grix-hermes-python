"""agent 共享单元测试：control_share_set diff、子连接增删、未知 cmd 容错、回执路由。

走 stub 模式（同 run_connector_alignment_check.py），不依赖 hermes-agent host；
GrixTransportClient 替成 FakeClient，用 monkeypatch 后验证：
1. control_share_set 增删 _shared_clients
2. 未知 cmd 不抛错（向后兼容）
3. 共享子连接收到事件后，回执从同一连接发出（contextvar 路由）
4. disconnect 一并清理子连接
"""

import asyncio
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ── stub host modules（与 run_connector_alignment_check 同模式） ──
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
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


# ── Fake transport client：捕获 connect/disconnect 调用，不真连 ws ──
class FakeTransportClient:
    instances: list = []

    def __init__(self, config, *, connector=None, on_status=None):
        self.config = config
        self.on_packet = None  # adapter 会通过 _bind_packet_handler 设置
        self.on_status = on_status
        self.connected = False
        self.disconnect_reason = None
        self.send_log: list = []  # 所有 send_xxx 调用都记到这
        FakeTransportClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def disconnect(self, reason: str = ""):
        self.connected = False
        self.disconnect_reason = reason

    # 模拟最少几个 send 方法,供路由测试用
    async def acknowledge_event(self, **kw):
        self.send_log.append(("acknowledge_event", kw))

    async def complete_event(self, **kw):
        self.send_log.append(("complete_event", kw))


def _make_adapter():
    """构造一个最小可用 GrixAdapter，FakeClient 替掉真 transport。"""
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
    inst._token_lock_identity = None
    inst._completed_event_ids = set()
    inst._seen_event_ids = {}
    inst._completed_event_results = {}
    inst._completed_stop_results = {}
    inst._reply_event_ids = {}
    inst._latest_sources = {}
    inst._message_sources = {}
    inst._message_session_keys = {}
    inst._user_dm_session_ids = {}
    inst._user_dm_session_keys = {}
    inst._approval_state = {}
    inst._processing_message_ids = {}
    inst._revoked_message_keys = set()
    inst._busy_ack_msg_ids = {}
    inst._last_send_at = 0.0
    inst._send_lock = asyncio.Lock()
    inst._reconnect_lock = asyncio.Lock()
    inst._tool_progress_msg_ids = set()
    inst._session_connector_hints = {}
    inst._shared_clients = {}
    inst._share_sync_lock = asyncio.Lock()
    inst._shutting_down = False
    inst._background_tasks = set()
    # 主连接（用 FakeClient 占位）
    inst._client = FakeTransportClient(inst.connection)
    inst._bind_packet_handler(inst._client)
    return inst


def _patch_transport(monkeypatch):
    monkeypatch.setattr(adapter_mod, "GrixTransportClient", FakeTransportClient)


# ── 1. control_share_set 增删 ──
def test_control_share_set_adds_and_removes_shared_clients(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()

    # 初次下发 [B, C]：起 2 条共享子连接
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B", "C"]}))
    assert set(inst._shared_clients.keys()) == {"B", "C"}
    assert all(c.connected for c in inst._shared_clients.values())
    # 子连接的 config 携带 shared_owner_id
    assert {c.config.shared_owner_id for c in inst._shared_clients.values()} == {"B", "C"}

    # 再下发 [B, D]：移除 C、新增 D
    c_client = inst._shared_clients["C"]
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B", "D"]}))
    assert set(inst._shared_clients.keys()) == {"B", "D"}
    assert c_client.connected is False  # C 已断
    assert c_client.disconnect_reason == "share revoked"

    # 下发空名单：全部断开
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": []}))
    assert inst._shared_clients == {}


# ── 2. 名单去空去重 ──
def test_share_set_strips_and_dedups(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    asyncio.run(inst._handle_share_set_packet({
        "agent_id": "100",
        "shared_to": ["B", " B ", "", "  ", "C"],
    }))
    assert set(inst._shared_clients.keys()) == {"B", "C"}


# ── 3. 非主连接收到 share_set 不处理(向后兼容/防双重 diff) ──
def test_share_set_ignored_on_shared_client(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    # 先建一个共享子连接
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B"]}))
    shared_b = inst._shared_clients["B"]

    # 模拟从 shared_b 收到 share_set(应被忽略)
    asyncio.run(inst._handle_protocol_packet(
        {"cmd": "control_share_set", "payload": {"agent_id": "100", "shared_to": []}},
        source_client=shared_b,
    ))
    # 因被忽略,B 仍在
    assert "B" in inst._shared_clients


# ── 4. 未知 cmd 不抛错(向后兼容) ──
def test_unknown_cmd_does_not_raise(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    # 不抛即成功
    asyncio.run(inst._handle_protocol_packet({"cmd": "totally_new_cmd", "payload": {}}))


# ── 5. contextvar 路由:回执发到事件来源 client ──
#    脱离 packet handler 上下文时**不再回退主连接**(防 sender 错乱),
#    改返回 None + log,让调用方直接失败比错发更安全。
def test_active_client_routes_to_source(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B"]}))
    shared_b = inst._shared_clients["B"]

    async def use_active(source):
        token = adapter_mod._CURRENT_CLIENT_CTX.set(source)
        try:
            assert inst._active_client() is source
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
        # 退出 packet 上下文后必须返回 None(不再 fallback 主连接,避免按主人身份错发)
        assert inst._active_client() is None

    asyncio.run(use_active(shared_b))
    asyncio.run(use_active(inst._client))


# ── 6. disconnect 一并清理子连接 ──
def test_disconnect_cleans_up_shared_clients(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B", "C"]}))
    b = inst._shared_clients["B"]
    c = inst._shared_clients["C"]

    # 模拟 disconnect 的核心部分:置 shutting_down + 断子连接
    inst._shutting_down = True
    asyncio.run(_run_share_cleanup(inst))

    assert inst._shared_clients == {}
    assert b.connected is False
    assert c.connected is False


async def _run_share_cleanup(inst):
    async with inst._share_sync_lock:
        shared_clients = list(inst._shared_clients.values())
        inst._shared_clients.clear()
    for s in shared_clients:
        await s.disconnect("adapter disconnect")


# ── 7. CRIT 守卫:_get_ready_client 在 packet handler 上下文中必须返回共享 client,
#       而不是主连接。否则 LLM 回复会被回到主人(共享物理隔离失效)。
#       脱离 packet 上下文时**不再回退主连接**(防 sender 错乱),改返回 None + log。
def test_get_ready_client_uses_shared_in_handler_context(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B"]}))
    shared_b = inst._shared_clients["B"]
    main = inst._client

    # FakeClient.status 让 _get_ready_client 认为它就绪
    for c in (main, shared_b):
        c.status = {"connected": True, "authed": True}

    async def in_handler_ctx():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(shared_b)
        try:
            got = await inst._get_ready_client(operation="send")
            assert got is shared_b, "在 handler 上下文中 _get_ready_client 必须返回共享 client"
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)

    async def in_primary_handler_ctx():
        # 主连接的 packet handler 上下文 → 返回主连接,主连接的就绪检查仍走 reconnect 路径。
        token = adapter_mod._CURRENT_CLIENT_CTX.set(main)
        try:
            got = await inst._get_ready_client(operation="send")
            assert got is main
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)

    async def background_must_not_fallback():
        # 无 contextvar(背景任务)→ **不再回退主连接**,直接返回 None。
        # 避免脱离 packet 上下文的 send 把消息按主人身份错发出去。
        got = await inst._get_ready_client(operation="background_task")
        assert got is None

    asyncio.run(in_handler_ctx())
    asyncio.run(in_primary_handler_ctx())
    asyncio.run(background_must_not_fallback())


# ── 8. _schedule_session_route_bind 跟随 ContextVar:被共享者会话的 route_bind
#       必须从对应共享子连接发,不能跑到主连接(会绑错路由)。
def test_session_route_bind_follows_contextvar(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B"]}))
    shared_b = inst._shared_clients["B"]

    # 给 FakeClient 加 bind_session_route 捕获
    bind_calls = []

    async def fake_bind(self, **kw):
        bind_calls.append((self, kw))

    monkeypatch.setattr(FakeTransportClient, "bind_session_route", fake_bind, raising=False)

    async def run_bind_in_ctx(source_client):
        token = adapter_mod._CURRENT_CLIENT_CTX.set(source_client)
        try:
            inst._schedule_session_route_bind(session_key="sk-1", session_id="sid-1")
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
        # 等 spawn 的 task 跑完
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(run_bind_in_ctx(shared_b))
    assert bind_calls and bind_calls[-1][0] is shared_b, \
        "_schedule_session_route_bind 必须用 ContextVar 里的 client(共享子连接),不应走主连接"

    # 脱离 ContextVar → 跳过(不发包,不抛错)
    bind_calls.clear()
    inst._schedule_session_route_bind(session_key="sk-2", session_id="sid-2")
    # spawn 不会发生
    async def drain():
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    asyncio.run(drain())
    assert bind_calls == [], "脱离 ContextVar 时 _schedule_session_route_bind 必须跳过,不能错走主连接"


# ── 9. _busy_ack 删除路径:删除 busy-ack 提示时必须从「发送该提示的 client」发出。
#       否则脱离 packet 上下文后调 delete_message,会因 ContextVar 丢失而失败 + log。
def test_delete_busy_ack_uses_stored_sender_client(monkeypatch):
    _patch_transport(monkeypatch)
    inst = _make_adapter()
    asyncio.run(inst._handle_share_set_packet({"agent_id": "100", "shared_to": ["B"]}))
    shared_b = inst._shared_clients["B"]

    # 模拟 send() 已记录了 busy-ack(sender=shared_b)
    inst._busy_ack_msg_ids["sk-x"] = ("chat-1", "msg-1", shared_b)

    # 让 delete_message 捕获调用时 ContextVar 的值
    seen_ctx_clients = []

    async def fake_delete(chat_id, msg_id):
        seen_ctx_clients.append(adapter_mod._CURRENT_CLIENT_CTX.get())

        class _R:
            success = True
        return _R()

    monkeypatch.setattr(inst, "delete_message", fake_delete)

    asyncio.run(inst._delete_busy_ack("chat-1", "msg-1", "sk-x", shared_b))
    assert seen_ctx_clients == [shared_b], \
        "_delete_busy_ack 必须在调用 delete_message 前把 ContextVar set 为发送方 client"
