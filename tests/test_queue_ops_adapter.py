"""队列协议操作的 adapter 级测试（queue_snapshot_query / queue_reorder /
queue_clear / event_cancel / event_stop 摘队 / 收口释放槽位）。

走 stub 模式（同 test_event_result_timing.py），不依赖 hermes-agent host。
队列投递被替换为记录桩：只验证协议语义与队列状态，不真正跑 hermes 轮次。
"""

import asyncio
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
        gw_platforms_base.ProcessingOutcome = type(
            "ProcessingOutcome", (), {"SUCCESS": object(), "CANCELLED": object()}
        )
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
from grix_hermes.event_queue import EventQueue, EventQueueConfig, QueueItem  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


class FakeTransportClient:
    """记录全部队列相关出包的 transport 假件。"""

    def __init__(self):
        self._config = SimpleNamespace(shared_owner_id=None)
        self.status = {"connected": True, "authed": True}
        self.completed = []
        self.event_states = []
        self.snapshots = []
        self.reorder_results = []
        self.clear_results = []
        self.cancel_results = []
        self.stop_acks = []
        self.stop_results = []

    async def complete_event(self, *, event_id, status, code=None, message=None, updated_at=None):
        self.completed.append({"event_id": event_id, "status": status, "message": message})

    async def send_event_state(self, *, event_id, session_id, state, extra=None):
        self.event_states.append(
            {"event_id": event_id, "session_id": session_id, "state": state, **(extra or {})}
        )

    async def send_queue_snapshot(self, *, session_id, running, running_items, queued):
        self.snapshots.append(
            {"session_id": session_id, "running": running, "queued": queued}
        )

    async def send_queue_reorder_result(self, *, session_id, applied_event_ids):
        self.reorder_results.append(
            {"session_id": session_id, "applied_event_ids": applied_event_ids}
        )

    async def send_queue_clear_result(self, *, session_id, success, canceled_event_ids=None, message=None):
        self.clear_results.append(
            {
                "session_id": session_id,
                "success": success,
                "canceled_event_ids": canceled_event_ids or [],
            }
        )

    async def send_event_cancel_result(self, *, event_id, accepted, reason=None, final_state=None):
        self.cancel_results.append(
            {"event_id": event_id, "accepted": accepted, "final_state": final_state, "reason": reason}
        )

    async def acknowledge_stop(self, *, event_id, accepted, stop_id=None, updated_at=None):
        self.stop_acks.append({"event_id": event_id, "accepted": accepted, "stop_id": stop_id})

    async def complete_stop(self, *, event_id, status, stop_id=None, code=None, message=None, updated_at=None):
        self.stop_results.append({"event_id": event_id, "status": status, "stop_id": stop_id})


def _make_adapter(client=None):
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.config = SimpleNamespace(extra={})
    inst.connection = GrixConnectionConfig(endpoint="ws://x", agent_id="100", api_key="k")
    from collections import defaultdict

    inst._owner_states = defaultdict(adapter_mod._OwnerState)
    inst._shared_clients = {}
    inst._shutting_down = False
    inst._disconnect_requested = False
    inst._client = client or FakeTransportClient()
    inst._pending_messages = adapter_mod._PendingMessagesDict(inst._on_pending_consumed)
    inst._active_sessions = {}
    inst._inflight_dispatch_event_ids = {}
    inst._event_queue = EventQueue(
        EventQueueConfig(max_queued=5, queue_timeout_ms=0),
        on_deliver=inst._on_queue_deliver,
        on_state_change=inst._on_queue_state_change,
    )
    # 投递桩：记录被投递的事件，不真正跑 hermes 轮次。
    inst._delivered = []

    async def _fake_dispatch(message, source, session_key):
        inst._delivered.append(message.event_id)

    inst._dispatch_grix_event = _fake_dispatch
    return inst


def _item(inst, event_id, session_id="s1", group_key="g1", owner_key="", text=""):
    message = SimpleNamespace(event_id=event_id, session_id=session_id, text=text or event_id)
    source = SimpleNamespace(chat_id=session_id, thread_id=None)
    return QueueItem(
        event_id=event_id,
        session_id=session_id,
        group_key=group_key,
        owner_key=owner_key,
        preview=text or event_id,
        payload=(message, source, group_key),
    )


async def _settle():
    for _ in range(8):
        await asyncio.sleep(0)


def _run_with_ctx(inst, client, coro):
    async def _wrapped():
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            result = await coro
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)
        await _settle()
        return result

    return asyncio.run(_wrapped())


# ── 快照查询 ────────────────────────────────────────────────────────────


def test_snapshot_query_returns_snapshot_even_when_empty():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _run_with_ctx(
        inst, client, inst._handle_queue_snapshot_query_packet({"session_id": "s1"})
    )
    assert client.snapshots[-1] == {"session_id": "s1", "running": [], "queued": []}


def test_snapshot_query_reports_running_and_queued():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1", text="first job"))
        inst._event_queue.submit(_item(inst, "e2", text="second job"))
        await _settle()
        await inst._handle_queue_snapshot_query_packet({"session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    snap = client.snapshots[-1]
    assert snap["running"] == ["e1"]
    assert [q["event_id"] for q in snap["queued"]] == ["e2"]
    assert snap["queued"][0]["position"] == 1
    assert snap["queued"][0]["actions"] == [{"type": "cancel"}]
    # 排队事件的 event_state 已上报（带位置与可取消操作）
    queued_states = [s for s in client.event_states if s["state"] == "queued"]
    assert queued_states and queued_states[-1]["event_id"] == "e2"
    assert queued_states[-1]["queue_position"] == 1
    assert queued_states[-1]["actions"] == [{"type": "cancel"}]


# ── 重排 ────────────────────────────────────────────────────────────────


def test_reorder_applies_and_reports_result():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        for eid in ("e1", "e2", "e3", "e4"):
            inst._event_queue.submit(_item(inst, eid))
        await inst._handle_queue_reorder_packet(
            {"session_id": "s1", "ordered_event_ids": ["e4", "e2", "gone"]}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.reorder_results == [
        {"session_id": "s1", "applied_event_ids": ["e4", "e2", "e3"]}
    ]
    snap = client.snapshots[-1]
    assert [q["event_id"] for q in snap["queued"]] == ["e4", "e2", "e3"]


# ── 清空 ────────────────────────────────────────────────────────────────


def test_queue_clear_cancels_queued_only():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        inst._event_queue.submit(_item(inst, "e3"))
        await inst._handle_queue_clear_packet({"session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    assert client.clear_results == [
        {"session_id": "s1", "success": True, "canceled_event_ids": ["e2", "e3"]}
    ]
    # 排队事件以 canceled 收口（event_result），运行中的 e1 不动
    canceled = {c["event_id"] for c in client.completed if c["status"] == "canceled"}
    assert canceled == {"e2", "e3"}
    assert inst._event_queue.is_running("e1")


# ── 单事件取消 ──────────────────────────────────────────────────────────


def test_event_cancel_removes_queued_item():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await inst._handle_event_cancel_packet({"event_id": "e2", "session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    assert client.cancel_results == [
        {"event_id": "e2", "accepted": True, "final_state": "canceled", "reason": None}
    ]
    assert [c for c in client.completed if c["event_id"] == "e2"] == [
        {"event_id": "e2", "status": "canceled", "message": "canceled by user"}
    ]
    assert not inst._event_queue.is_queued("e2")
    assert inst._event_queue.is_running("e1")


def test_event_cancel_running_stops_only_that_group():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    stopped = []

    async def _fake_force_stop(source, session_key, **kw):
        stopped.append(session_key)
        return True

    inst._force_stop_session = _fake_force_stop

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await inst._handle_event_cancel_packet({"event_id": "e1", "session_id": "s1"})

    _run_with_ctx(inst, client, _flow())
    assert stopped == ["g1"]
    assert client.cancel_results[-1]["accepted"] is True
    assert client.cancel_results[-1]["final_state"] == "canceled"
    # e1 已被兜底收口为 canceled，槽位释放后 e2 被续投
    assert {"event_id": "e1", "status": "canceled", "message": "event canceled by user"} in client.completed
    assert inst._delivered[-1] == "e2"


def test_event_cancel_unknown_event_rejected():
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _run_with_ctx(
        inst, client, inst._handle_event_cancel_packet({"event_id": "nope", "session_id": "s1"})
    )
    assert client.cancel_results == [
        {
            "event_id": "nope",
            "accepted": False,
            "final_state": None,
            "reason": "event not found or not cancelable",
        }
    ]


# ── event_stop 精确摘除排队事件 ─────────────────────────────────────────


def test_event_stop_removes_queued_event_silently():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await inst._handle_stop_packet(
            {"event_id": "e2", "session_id": "s1", "stop_id": "stop-1"}
        )

    _run_with_ctx(inst, client, _flow())
    assert client.stop_acks == [{"event_id": "e2", "accepted": True, "stop_id": "stop-1"}]
    assert client.stop_results == [{"event_id": "e2", "status": "stopped", "stop_id": "stop-1"}]
    # 静默摘除：不发 canceled 终态（对齐 connector），运行中的 e1 不受影响
    assert all(c["event_id"] != "e2" for c in client.completed)
    assert inst._event_queue.is_running("e1")
    assert not inst._event_queue.is_queued("e2")


# ── 收口即释放槽位并续投 ─────────────────────────────────────────────────


def test_complete_event_releases_slot_and_drains_next():
    client = FakeTransportClient()
    inst = _make_adapter(client)

    async def _flow():
        inst._event_queue.submit(_item(inst, "e1"))
        inst._event_queue.submit(_item(inst, "e2"))
        await _settle()
        assert inst._delivered == ["e1"]
        await inst._complete_event_if_needed("e1", status="responded")

    _run_with_ctx(inst, client, _flow())
    assert inst._delivered == ["e1", "e2"]
    assert inst._event_queue.is_running("e2")
    assert {"event_id": "e1", "status": "responded", "message": None} in client.completed
    # e2 进入 running 的状态已上报
    running_states = [s for s in client.event_states if s["state"] == "running"]
    assert any(s["event_id"] == "e2" for s in running_states)
