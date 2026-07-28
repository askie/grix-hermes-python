"""工具栏停止定位 + composing 心跳收口守卫的单元测试。

两个缺陷共用一个根子：会话里"谁在干活"不能靠按 chat_type 拼出来的 session_key 猜。

1. 工具栏停止：后端 SendStopText 下发的 /stop 事件只带 session_id + sender，
   没有会话类型（DelegateEventPayload 无 session_type），归一化后一律是 dm，
   拼出的 key 在群会话（<ns>:grix:group:<session_id>:<user_id>）里永远对不上，
   停止按钮点了等于没点。改按 session_id 找该会话下所有活跃任务。

2. composing 心跳：hermes 的"正在输入"循环按 chat 起、只在所属后台任务收尾时
   取消，同一会话多轮并发时可能残留空转循环，每 2s 把 composing 续起来，任务
   早已结束前端却一直转。对齐 connector（event_result 上报即停止续期）：事件
   收口后不再点亮 composing。

走 stub 模式（同 test_event_result_timing.py），不依赖 hermes-agent host。
"""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_event_result_timing import (  # noqa: E402  (装好 stub 后再导入 adapter)
    FakeTransportClient,
    _make_adapter,
    _with_ctx,
    adapter_mod,
)

SESSION_ID = "3f5379be-aea1-4b8a-a0c6-7fe2bb12c1cf"
OWNER_ID = "2030840865701756928"
GROUP_KEY = f"agent:main:grix:group:{SESSION_ID}:{OWNER_ID}"
DM_KEY = f"agent:main:grix:dm:{SESSION_ID}"


def _session_key_like_hermes(source, **kw):
    """按 hermes gateway.session.build_session_key 的真实规则拼 key（够用的子集）。"""
    if source.chat_type == "dm":
        return f"agent:main:grix:dm:{source.chat_id}"
    parts = ["agent:main:grix", source.chat_type, source.chat_id]
    if source.thread_id:
        parts.append(source.thread_id)
    if source.user_id:
        parts.append(str(source.user_id))
    return ":".join(parts)


def _prepare_stop_adapter(monkeypatch, client, active_keys):
    """装一个能跑通 /stop 分支的 adapter：活跃任务已在跑，其余依赖打桩。"""
    monkeypatch.setattr(adapter_mod, "build_session_key", _session_key_like_hermes)
    inst = _make_adapter(client)
    inst.build_source = lambda **kw: SimpleNamespace(
        chat_id=kw.get("chat_id"),
        chat_type=kw.get("chat_type", "dm"),
        user_id=kw.get("user_id"),
        thread_id=kw.get("thread_id"),
    )
    for key in active_keys:
        inst._active_sessions[key] = asyncio.Event()

    inst._schedule_session_route_bind = lambda **kw: None
    inst.stopped_keys = []
    inst.confirmations = []

    async def _ack(**kw):
        return None

    client.acknowledge_event = _ack

    async def _cancel(session_key, **kw):
        inst.stopped_keys.append(session_key)
        inst._active_sessions.pop(session_key, None)

    async def _send_with_retry(**kw):
        inst.confirmations.append(kw.get("content"))
        return SimpleNamespace(success=True, message_id="m-1")

    inst.cancel_session_processing = _cancel
    inst._send_with_retry = _send_with_retry
    return inst


def _toolbar_stop_payload():
    """后端工具栏停止按钮下发的 /stop：无 session_type ⇒ 归一化成 dm。"""
    return {
        "event_id": "toolbar_cmd_1784293963630321971",
        "session_id": SESSION_ID,
        "msg_id": "9001",
        "sender_id": OWNER_ID,
        "event_type": "user_chat",
        "content": "/stop",
    }


# ── 1. 工具栏停止能停到群会话里的活跃任务 ────────────────────────────────────


def test_toolbar_stop_reaches_group_session(monkeypatch):
    """回归：/stop 事件拼出 dm key，但真实任务挂在 group key 上，仍必须停掉。"""
    client = FakeTransportClient()
    inst = _prepare_stop_adapter(monkeypatch, client, [GROUP_KEY])

    _with_ctx(client, inst._handle_message_packet(_toolbar_stop_payload()))

    assert inst.stopped_keys == [GROUP_KEY]
    assert inst.confirmations == ["⚡ Stopped. You can continue this session."]
    # /stop 事件自身按 responded 收口
    assert client.completed == [
        {"event_id": "toolbar_cmd_1784293963630321971", "status": "responded", "message": None}
    ]


def test_toolbar_stop_stops_every_parallel_run_with_one_confirmation(monkeypatch):
    """群里多个发起人各一路任务：全停，但只回一条确认。"""
    other_key = f"agent:main:grix:group:{SESSION_ID}:2054144943403827200"
    client = FakeTransportClient()
    inst = _prepare_stop_adapter(monkeypatch, client, [GROUP_KEY, other_key])

    _with_ctx(client, inst._handle_message_packet(_toolbar_stop_payload()))

    assert sorted(inst.stopped_keys) == sorted([GROUP_KEY, other_key])
    assert inst.confirmations == ["⚡ Stopped. You can continue this session."]


def test_toolbar_stop_leaves_other_sessions_alone(monkeypatch):
    """别的会话的任务不能被误停。"""
    foreign_key = "agent:main:grix:group:99999999-0000-0000-0000-000000000000:2030840865701756928"
    client = FakeTransportClient()
    inst = _prepare_stop_adapter(monkeypatch, client, [foreign_key])

    _with_ctx(client, inst._handle_message_packet(_toolbar_stop_payload()))

    assert inst.stopped_keys == []
    assert inst.confirmations == []
    assert foreign_key in inst._active_sessions


def test_toolbar_stop_clears_bg_hold_when_no_active_turn(monkeypatch):
    """回归：轮次已结束但 bg-hold 仍虚拟 running 时，/stop 必须清掉并给确认。"""
    client = FakeTransportClient()
    # 无活跃轮次 —— 复现「点停止等于没点」
    inst = _prepare_stop_adapter(monkeypatch, client, [])
    inst._state_for("").toolbar_active_work[DM_KEY] = {
        "session_id": SESSION_ID,
        "title": "node dist/grix.js",
        "bg_hold": True,
    }
    inst.killed_keys = []

    def _kill(session_key):
        inst.killed_keys.append(session_key)
        return 1

    inst._kill_session_bg_processes = _kill

    _with_ctx(client, inst._handle_message_packet(_toolbar_stop_payload()))

    assert inst.stopped_keys == []
    assert DM_KEY in inst.killed_keys
    assert DM_KEY not in inst._state_for("").toolbar_active_work
    assert inst.confirmations == ["⚡ Stopped. You can continue this session."]
    assert client.completed == [
        {"event_id": "toolbar_cmd_1784293963630321971", "status": "responded", "message": None}
    ]


def test_toolbar_stop_kills_bg_even_with_active_turn(monkeypatch):
    """有活跃轮次时 /stop 既停轮次，也杀同会话后台进程。"""
    client = FakeTransportClient()
    inst = _prepare_stop_adapter(monkeypatch, client, [DM_KEY])
    inst._state_for("").toolbar_active_work[DM_KEY] = {
        "session_id": SESSION_ID,
        "title": "long job",
        "bg_hold": True,
    }
    inst.killed_keys = []
    inst._kill_session_bg_processes = lambda session_key: (
        inst.killed_keys.append(session_key) or 1
    )

    _with_ctx(client, inst._handle_message_packet(_toolbar_stop_payload()))

    assert inst.stopped_keys == [DM_KEY]
    assert DM_KEY in inst.killed_keys
    assert DM_KEY not in inst._state_for("").toolbar_active_work
    assert inst.confirmations == ["⚡ Stopped. You can continue this session."]


def test_toolbar_stop_does_not_confirm_when_nothing_to_clear(monkeypatch):
    """无活跃轮次、无 bg-hold、无后台进程：不发停止确认。"""
    client = FakeTransportClient()
    inst = _prepare_stop_adapter(monkeypatch, client, [])
    inst._kill_session_bg_processes = lambda session_key: 0

    _with_ctx(client, inst._handle_message_packet(_toolbar_stop_payload()))

    assert inst.stopped_keys == []
    assert inst.confirmations == []
    assert client.completed == [
        {"event_id": "toolbar_cmd_1784293963630321971", "status": "responded", "message": None}
    ]


# ── 2. session_key 归属判定 ─────────────────────────────────────────────────


def test_session_key_belongs_to_session_matches_every_shape():
    belongs = adapter_mod.GrixAdapter._session_key_belongs_to_session
    thread_key = f"agent:main:grix:group:{SESSION_ID}:019f6fb3-93cd-71a1-af17-7f7d4f6b3faf"
    assert belongs(GROUP_KEY, SESSION_ID)
    assert belongs(DM_KEY, SESSION_ID)
    assert belongs(thread_key, SESSION_ID)


def test_session_key_belongs_to_session_rejects_other_sessions():
    belongs = adapter_mod.GrixAdapter._session_key_belongs_to_session
    other = "99999999-0000-0000-0000-000000000000"
    assert not belongs(GROUP_KEY, other)
    assert not belongs("", SESSION_ID)
    assert not belongs(GROUP_KEY, "")
    # 会话 id 只按完整段匹配，不能被前缀/子串蒙混过去
    assert not belongs(GROUP_KEY, SESSION_ID[:8])
    assert not belongs(f"agent:main:grix:dm:{SESSION_ID}-suffix", SESSION_ID)


# ── 3. composing 心跳的收口守卫 ─────────────────────────────────────────────


def _prepare_typing_adapter(client):
    inst = _make_adapter(client)
    inst.activities = []

    async def _set_activity(**kw):
        inst.activities.append(kw)

    client.set_session_activity = _set_activity
    return inst


def _patch_resolve(monkeypatch):
    async def _resolve(client, connection, chat_id, **kw):
        return chat_id, None

    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve)


def test_composing_sent_while_event_unsettled(monkeypatch):
    """任务在跑（事件已登记未收口）：心跳正常点亮 composing。"""
    _patch_resolve(monkeypatch)
    client = FakeTransportClient()
    inst = _prepare_typing_adapter(client)
    inst._owner_states[""].session_open_event_ids[GROUP_KEY] = ["ev-1"]

    _with_ctx(client, inst.send_typing(SESSION_ID))

    assert len(inst.activities) == 1
    assert inst.activities[0]["kind"] == "composing"
    assert inst.activities[0]["active"] is True


def test_composing_skipped_after_events_settled(monkeypatch):
    """回归：事件已收口后，空转的心跳循环不得再把 composing 续起来。"""
    _patch_resolve(monkeypatch)
    client = FakeTransportClient()
    inst = _prepare_typing_adapter(client)
    # 没有任何登记事件 = 本会话的活已经收口
    _with_ctx(client, inst.send_typing(SESSION_ID))

    assert inst.activities == []


def test_composing_tracks_running_and_next_run_registries(monkeypatch):
    """running / next_run 登记同样算"还在干活"。"""
    _patch_resolve(monkeypatch)
    for registry_name in ("session_running_event_ids", "session_next_run_event_ids"):
        client = FakeTransportClient()
        inst = _prepare_typing_adapter(client)
        getattr(inst._owner_states[""], registry_name)[GROUP_KEY] = ["ev-1"]

        _with_ctx(client, inst.send_typing(SESSION_ID))

        assert len(inst.activities) == 1, registry_name


def test_composing_skipped_for_other_sessions_events(monkeypatch):
    """别的会话在跑，不能把本会话点亮。"""
    _patch_resolve(monkeypatch)
    client = FakeTransportClient()
    inst = _prepare_typing_adapter(client)
    foreign_key = "agent:main:grix:group:99999999-0000-0000-0000-000000000000:1"
    inst._owner_states[""].session_open_event_ids[foreign_key] = ["ev-1"]

    _with_ctx(client, inst.send_typing(SESSION_ID))

    assert inst.activities == []
