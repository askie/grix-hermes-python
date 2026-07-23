"""complete() 续投必须在 on_processing_complete 返回前完成。

回归 de89f921（2026-07-23）：上一轮 complete → create_task(deliver) 晚于
hermes base 的 late_pending 检查，追问被 busy 打进 pending 后无人捞取，
EventQueue running 槽空挂 30 分钟被 run_timeout 收割，用户看到
「消息处理中断」。

本文件锁住：
1. ``_complete_event_if_needed`` 同步 await 续投（deliver 在 complete 返回前结束）；
2. busy-pending 孤儿在会话空闲后被恢复强制开跑。
"""

from __future__ import annotations

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

        gw_cfg.Platform = _Platform
        gw_cfg.PlatformConfig = lambda **kw: SimpleNamespace(**kw)

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
from grix_hermes.contract import STATUS_RESPONDED  # noqa: E402
from grix_hermes.event_queue import EventQueue, EventQueueConfig, QueueItem  # noqa: E402
from grix_hermes.protocol import GrixConnectionConfig  # noqa: E402


class FakeClient:
    def __init__(self):
        self.completed = []

    async def complete_event(self, *, event_id, status, message=None, updated_at=None):
        self.completed.append({"event_id": event_id, "status": status, "message": message})


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
    inst._client = client or FakeClient()
    inst._pending_messages = adapter_mod._PendingMessagesDict(inst._on_pending_consumed)
    inst._active_sessions = {}
    inst._inflight_dispatch_event_ids = {}
    inst._sync_deliver_bucket = None
    inst._background_tasks = set()
    inst._event_queue = EventQueue(
        EventQueueConfig(max_queued=5, queue_timeout_ms=0, run_timeout_ms=0),
        on_deliver=inst._on_queue_deliver,
        on_state_change=inst._on_queue_state_change,
    )
    inst._delivered = []
    inst._deliver_started = []

    async def _fake_dispatch(message, source, session_key):
        inst._deliver_started.append(message.event_id)
        # 模拟 busy：挂进 pending，不认领轮次（开放事件仍 open）。
        if getattr(inst, "_park_as_pending", False):
            event = SimpleNamespace(
                text=message.text,
                message_id=getattr(message, "message_id", message.event_id),
                raw_message={"event_id": message.event_id, "_grix_kind": "message"},
                source=source,
            )
            open_ids = inst._active_state().session_open_event_ids.setdefault(session_key, [])
            if message.event_id not in open_ids:
                open_ids.append(message.event_id)
            inst._pending_messages[session_key] = event
            return
        inst._delivered.append(message.event_id)
        open_ids = inst._active_state().session_open_event_ids.setdefault(session_key, [])
        if message.event_id in open_ids:
            open_ids.remove(message.event_id)
        await inst._complete_event_if_needed(message.event_id, status=STATUS_RESPONDED)

    inst._dispatch_grix_event = _fake_dispatch

    async def _noop_snapshot(*a, **k):
        return None

    inst._push_queue_snapshot = _noop_snapshot

    async def _ready(**kw):
        return inst._client

    inst._get_ready_client = _ready
    return inst


def _item(event_id, session_id="s1", group_key="sk:s1", text=""):
    message = SimpleNamespace(
        event_id=event_id,
        session_id=session_id,
        text=text or event_id,
        message_id=f"m-{event_id}",
    )
    source = SimpleNamespace(chat_id=session_id, thread_id=None)
    return QueueItem(
        event_id=event_id,
        session_id=session_id,
        group_key=group_key,
        owner_key="",
        preview=text or event_id,
        payload=(message, source, group_key),
        content=text or event_id,
    )


def test_complete_awaits_next_deliver_before_returning():
    """续投在 complete 返回前结束（不再依赖 create_task 事后调度）。"""

    async def _run():
        inst = _make_adapter()
        # e1 running，e2 queued；complete(e1) 应同步投递 e2
        inst._event_queue.submit(_item("e1"))
        inst._event_queue.submit(_item("e2"))
        assert inst._event_queue.is_running("e1")
        assert inst._event_queue.is_queued("e2")

        await inst._complete_event_if_needed("e1", status=STATUS_RESPONDED)

        # complete 返回时 e2 必须已经走过 deliver（旧 bug：此时 deliver 还没跑）
        assert "e2" in inst._deliver_started
        assert "e2" in inst._delivered
        return inst

    asyncio.run(_run())


def test_complete_sync_deliver_parks_pending_before_return():
    """会话仍 busy 时，续投进 pending 必须发生在 complete 返回前。"""

    async def _run():
        inst = _make_adapter()
        inst._park_as_pending = True
        sk = "sk:s1"
        # 模拟上一轮仍占着 active session（late_pending 检查前的窗口）
        inst._active_sessions[sk] = asyncio.Event()
        inst._event_queue.submit(_item("e1", group_key=sk))
        inst._event_queue.submit(_item("e2", group_key=sk))

        await inst._complete_event_if_needed("e1", status=STATUS_RESPONDED)

        assert "e2" in inst._deliver_started
        assert sk in inst._pending_messages
        assert inst._event_still_open(sk, "e2")
        assert inst._event_queue.is_running("e2")
        return inst

    asyncio.run(_run())


def test_orphaned_pending_recovery_starts_turn_when_idle():
    """会话已空闲、pending 仍挂着 → 恢复任务强制 handle_message。"""

    async def _run():
        inst = _make_adapter()
        sk = "sk:s1"
        event_id = "e-orphan"
        handled = []

        async def _handle(event):
            handled.append(getattr(event, "message_id", None))
            open_ids = inst._active_state().session_open_event_ids.get(sk, [])
            if event_id in open_ids:
                open_ids.remove(event_id)
            # 不走真实 EQ complete，避免 stub deliver 干扰
            inst._event_queue._running.pop(event_id, None)

        inst.handle_message = _handle
        inst._active_state().session_open_event_ids[sk] = [event_id]
        inst._pending_messages[sk] = SimpleNamespace(
            text="follow-up",
            message_id="m-orphan",
            raw_message={"event_id": event_id, "_grix_kind": "message"},
            source=SimpleNamespace(chat_id="s1"),
        )
        # 直接塞 running 槽，不走 submit（submit 会触发 on_deliver）
        inst._event_queue._running[event_id] = _item(event_id, group_key=sk)
        assert inst._event_queue.is_running(event_id)

        await inst._recover_orphaned_pending(sk, event_id)

        assert handled == ["m-orphan"]
        assert sk not in inst._pending_messages
        return inst

    asyncio.run(_run())
