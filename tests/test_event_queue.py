"""EventQueue 单元测试（纯内存队列，无 I/O）。

覆盖：提交/立即投递/排队、组内串行与跨组并行、完成续投、单删（取消 /
静默摘除）、按会话清空、愿望清单重排、快照、队满拒绝、排队超时、
暂停/恢复闸门。
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.event_queue import (  # noqa: E402
    SUBMIT_QUEUED,
    SUBMIT_REJECTED,
    SUBMIT_RUNNING,
    STATE_CANCELED,
    STATE_FAILED,
    STATE_QUEUED,
    STATE_RUNNING,
    EventQueue,
    EventQueueConfig,
    QueueItem,
    build_preview,
)


class Recorder:
    def __init__(self):
        self.delivered = []
        self.states = []

    def on_deliver(self, item):
        self.delivered.append(item.event_id)

    def on_state_change(self, item, state, meta):
        self.states.append((item.event_id, state, dict(meta)))


def _make_queue(max_queued=5, queue_timeout_ms=0):
    rec = Recorder()
    queue = EventQueue(
        EventQueueConfig(max_queued=max_queued, queue_timeout_ms=queue_timeout_ms),
        on_deliver=rec.on_deliver,
        on_state_change=rec.on_state_change,
    )
    return queue, rec


def _item(event_id, session_id="s1", group_key="g1", owner_key="", preview="", content=""):
    return QueueItem(
        event_id=event_id,
        session_id=session_id,
        group_key=group_key,
        owner_key=owner_key,
        preview=preview or event_id,
        payload=None,
        content=content,
    )


# ── 提交与调度 ──────────────────────────────────────────────────────────


def test_submit_idle_delivers_immediately():
    queue, rec = _make_queue()
    assert queue.submit(_item("e1")) == SUBMIT_RUNNING
    assert rec.delivered == ["e1"]
    assert rec.states == [("e1", STATE_RUNNING, {})]
    assert queue.is_running("e1")


def test_submit_busy_group_queues_with_position():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    assert queue.submit(_item("e2")) == SUBMIT_QUEUED
    assert queue.submit(_item("e3")) == SUBMIT_QUEUED
    assert rec.delivered == ["e1"]
    queued_states = [s for s in rec.states if s[1] == STATE_QUEUED]
    # 入队只通知新项（对齐 connector）：e2=1/1、e3=2/2，不广播全会话
    assert queued_states == [
        ("e2", STATE_QUEUED, {"queue_position": 1, "queue_total": 1}),
        ("e3", STATE_QUEUED, {"queue_position": 2, "queue_total": 2}),
    ]


def test_complete_drains_next_in_fifo_order():
    queue, rec = _make_queue()
    for eid in ("e1", "e2", "e3"):
        queue.submit(_item(eid))
    queue.complete("e1")  # 无运行 loop → 同步续投
    assert rec.delivered == ["e1", "e2"]
    queue.complete("e2")
    assert rec.delivered == ["e1", "e2", "e3"]
    queue.complete("e3")
    assert queue.running_count == 0 and queue.queued_count == 0


def test_groups_do_not_block_each_other():
    queue, rec = _make_queue()
    queue.submit(_item("a1", group_key="ga"))
    queue.submit(_item("a2", group_key="ga"))
    # ga 拥塞不影响 gb 立即投递
    assert queue.submit(_item("b1", group_key="gb")) == SUBMIT_RUNNING
    assert rec.delivered == ["a1", "b1"]
    # a1 完成后续投的是 a2（跳过运行中的 gb）
    queue.complete("a1")
    assert rec.delivered == ["a1", "b1", "a2"]


def test_duplicate_submit_is_ignored():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    assert queue.submit(_item("e1")) == SUBMIT_QUEUED
    assert rec.delivered == ["e1"]
    assert queue.queued_count == 0


def test_queue_full_rejects_with_failed_state():
    queue, rec = _make_queue(max_queued=1)
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    assert queue.submit(_item("e3")) == SUBMIT_REJECTED
    assert ("e3", STATE_FAILED, {"reason": "queue full"}) in rec.states
    assert queue.queued_count == 1


# ── 单删 / 清空 / 重排 ──────────────────────────────────────────────────


def test_cancel_queued_removes_and_reports_canceled():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    queue.submit(_item("e3"))
    assert queue.cancel_queued("e2") is True
    assert not queue.is_queued("e2")
    assert ("e2", STATE_CANCELED, {"reason": "canceled by user"}) in rec.states
    # e3 位置刷新为 1/1
    assert rec.states[-1] == ("e3", STATE_QUEUED, {"queue_position": 1, "queue_total": 1})
    # 运行中的不能走 cancel_queued
    assert queue.cancel_queued("e1") is False


def test_remove_queued_is_silent():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    removed = queue.remove_queued("e2")
    assert removed is not None and removed.event_id == "e2"
    assert not any(state == STATE_CANCELED for _, state, _ in rec.states)
    assert queue.remove_queued("e1") is None  # 运行中不摘
    assert queue.remove_queued("missing") is None


def test_clear_only_targets_session_and_owner():
    queue, rec = _make_queue()
    queue.submit(_item("e1", session_id="s1"))
    queue.submit(_item("e2", session_id="s1"))
    queue.submit(_item("x1", session_id="s2", group_key="g2"))
    queue.submit(_item("x2", session_id="s2", group_key="g2"))
    queue.submit(_item("o1", session_id="s1", owner_key="other"))
    cleared = queue.clear("s1", "", reason="canceled by queue clear")
    # 只清主 owner 名下 s1 的排队项：跨会话（x2）与其他 owner（o1）都不动，
    # 运行中的 e1 也不动。
    assert [item.event_id for item in cleared] == ["e2"]
    assert ("e2", STATE_CANCELED, {"reason": "canceled by queue clear"}) in rec.states
    assert queue.is_queued("x2") and queue.is_queued("o1")
    assert queue.is_running("e1")


def test_reorder_wishlist_semantics():
    queue, _rec = _make_queue()
    queue.submit(_item("e1"))
    for eid in ("e2", "e3", "e4"):
        queue.submit(_item(eid))
    # 期望顺序含未知 id（已出队）与缺失项（e2 未列出 → 排尾）
    applied = queue.reorder("s1", ["e4", "gone", "e3"], "")
    assert applied == ["e4", "e3", "e2"]
    queue.complete("e1")
    queue.complete("e4")
    queue.complete("e3")
    snap = queue.snapshot("s1", "")
    assert snap["running"] == ["e2"]


def test_reorder_keeps_other_sessions_slots():
    queue, rec = _make_queue()
    queue.submit(_item("a1", session_id="sa", group_key="ga"))
    queue.submit(_item("b1", session_id="sb", group_key="gb"))
    queue.submit(_item("a2", session_id="sa", group_key="ga"))
    queue.submit(_item("b2", session_id="sb", group_key="gb"))
    queue.submit(_item("a3", session_id="sa", group_key="ga"))
    # 全局队列 [a2, b2, a3] → 重排 sa 段为 [a3, a2]，b2 槽位不动
    applied = queue.reorder("sa", ["a3", "a2"], "")
    assert applied == ["a3", "a2"]
    queue.complete("b1")
    assert rec.delivered[-1] == "b2"  # b 组续投不受 sa 重排影响
    queue.complete("a1")
    assert rec.delivered[-1] == "a3"  # sa 段队头已换成 a3


def test_reorder_empty_session_returns_empty():
    queue, _rec = _make_queue()
    assert queue.reorder("nope", ["x"], "") == []


# ── 快照 ────────────────────────────────────────────────────────────────


def test_snapshot_shape():
    queue, _rec = _make_queue()
    queue.submit(_item("e1", preview="first"))
    queue.submit(_item("e2", preview="second"))
    queue.submit(_item("x1", session_id="s2", group_key="g2"))
    snap = queue.snapshot("s1", "")
    assert snap["running"] == ["e1"]
    assert snap["running_items"] == [{"event_id": "e1", "content_preview": "first"}]
    assert snap["queued"] == [
        {
            "event_id": "e2",
            "position": 1,
            "content_preview": "second",
            "content": "",
            "held": False,
            "held_reason": "",
        }
    ]
    empty = queue.snapshot("unknown", "")
    assert empty == {"running": [], "running_items": [], "queued": []}


# ── 超时 / 暂停恢复 ─────────────────────────────────────────────────────


def test_queue_timeout_fails_event():
    async def _run():
        queue, rec = _make_queue(queue_timeout_ms=30)
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        await asyncio.sleep(0.1)
        return queue, rec

    queue, rec = asyncio.run(_run())
    assert ("e2", STATE_FAILED, {"reason": "queue timeout"}) in rec.states
    assert queue.queued_count == 0


def test_pause_resume_gate():
    queue, rec = _make_queue()
    queue.pause("restart")
    queue.pause("barrier")
    assert queue.submit(_item("e1")) == SUBMIT_QUEUED
    assert rec.delivered == []
    queue.resume("restart")
    assert rec.delivered == []  # 还有 barrier 原因，闸门未开
    queue.resume("barrier")
    assert rec.delivered == ["e1"]


def test_drain_all_queued_is_silent_and_complete():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    queue.submit(_item("x1", session_id="s2", group_key="g2"))
    queue.submit(_item("x2", session_id="s2", group_key="g2"))
    drained = queue.drain_all_queued()
    assert [item.event_id for item in drained] == ["e2", "x2"]
    assert queue.queued_count == 0
    assert not any(state in (STATE_CANCELED, STATE_FAILED) for _, state, _ in rec.states)


def test_session_refs_covers_running_and_queued():
    queue, _rec = _make_queue()
    queue.submit(_item("e1", session_id="s1"))
    queue.submit(_item("e2", session_id="s1"))
    queue.submit(_item("x1", session_id="s2", group_key="g2", owner_key="other"))
    assert queue.session_refs() == [("s1", ""), ("s2", "other")]


def test_build_preview():
    assert build_preview("  hello   world  ") == "hello world"
    assert build_preview("") == "Message"
    assert build_preview(None) == "Message"
    long = "x" * 100
    assert build_preview(long) == "x" * 64 + "..."


# ── hold / release / edit ───────────────────────────────────────────────


def test_hold_blocks_group_head_until_release():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    queue.submit(_item("e3"))
    assert queue.hold("e2", reason="manual") is True
    queue.complete("e1")  # 无运行 loop → 同步续投
    # held 项排到组队头：整组原地等待，e3 不跳过、不变序
    assert rec.delivered == ["e1"]
    assert queue.is_queued("e2") and queue.is_queued("e3")
    assert queue.release("e2") is True
    assert rec.delivered == ["e1", "e2"]
    item = queue.find("e2")
    assert item.held is False and item.held_reason == ""


def test_reorder_away_held_head_unblocks_queue():
    queue, rec = _make_queue()
    queue.submit(_item("e0"))
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    assert queue.hold("e1", reason="editing") is True
    queue.complete("e0")  # drain 遇 held 组队首 e1 停住
    assert rec.delivered == ["e0"]
    # 用户把 held 的 e1 拖到队尾，e2 成为可执行的新队首：reorder 必须补 drain
    queue.reorder("s1", ["e2", "e1"])
    assert rec.delivered == ["e0", "e2"]
    assert queue.is_queued("e1")


def test_held_group_does_not_block_other_groups():
    queue, rec = _make_queue()
    queue.pause("setup")
    queue.submit(_item("a1", group_key="ga"))
    queue.submit(_item("b1", group_key="gb"))
    assert queue.hold("a1", reason="manual") is True
    queue.resume("setup")
    # ga 队首被 hold 只阻塞 ga，gb 照常出队
    assert rec.delivered == ["b1"]
    assert queue.is_queued("a1")
    queue.release("a1")
    assert rec.delivered == ["b1", "a1"]


def test_hold_release_edit_only_hit_queued():
    queue, _rec = _make_queue()
    queue.submit(_item("e1"))
    assert queue.hold("e1", reason="manual") is False  # 运行中不可 hold
    assert queue.hold("missing", reason="manual") is False
    assert queue.release("e1") is False
    assert queue.edit("e1", "x") is None
    assert queue.edit("missing", "x") is None


def test_release_on_unheld_queued_item_is_idempotent_ok():
    queue, _rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2"))
    assert queue.release("e2") is True
    assert queue.find("e2").held is False


def test_edit_rewrites_content_preview_and_auto_releases():
    queue, rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2", content="旧内容"))
    assert queue.hold("e2", reason="editing") is True
    item = queue.edit("e2", "改后的  全文内容")
    assert item is not None and item.event_id == "e2"
    assert item.content == "改后的  全文内容"
    assert item.preview == build_preview("改后的  全文内容")
    assert item.held is False and item.held_reason == ""
    # 编辑自动解除 hold：e1 完成后 e2 正常出队
    queue.complete("e1")
    assert rec.delivered == ["e1", "e2"]


def test_snapshot_carries_content_and_held():
    queue, _rec = _make_queue()
    queue.submit(_item("e1"))
    queue.submit(_item("e2", content="task full text"))
    assert queue.hold("e2", reason="manual") is True
    entry = queue.snapshot("s1", "")["queued"][0]
    assert entry["content"] == "task full text"
    assert entry["held"] is True
    assert entry["held_reason"] == "manual"


def test_hold_ttl_auto_release(monkeypatch):
    import grix_hermes.event_queue as eq_mod

    monkeypatch.setattr(eq_mod, "HOLD_TTL_MIN_MS", 1)

    async def _run():
        queue, rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        assert queue.hold("e2", reason="editing", ttl_ms=30) is True
        queue.complete("e1")
        await asyncio.sleep(0.01)
        assert rec.delivered == ["e1"]  # TTL 未到，仍被 hold 挡住
        await asyncio.sleep(0.2)
        return queue, rec

    queue, rec = asyncio.run(_run())
    # TTL 到期自动放行：e2 被续投，定时器无残留
    assert rec.delivered == ["e1", "e2"]
    assert queue.is_running("e2")
    assert not queue._hold_handles


def test_hold_ttl_clamped_to_minimum():
    """ttl_ms 低于下限被 clamp 到 60s：短 sleep 后不会被放行。"""

    async def _run():
        queue, rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        assert queue.hold("e2", reason="editing", ttl_ms=10) is True
        handle = queue._hold_handles["e2"]
        loop = asyncio.get_running_loop()
        # 定时器至少挂在 60s 之后（clamp 生效），而不是 10ms
        assert handle.when() - loop.time() > 50
        queue.destroy()

    asyncio.run(_run())


def test_repeat_hold_resets_ttl_timer():
    async def _run():
        queue, _rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        assert queue.hold("e2", reason="editing", ttl_ms=60_000) is True
        h1 = queue._hold_handles["e2"]
        assert queue.hold("e2", reason="editing", ttl_ms=60_000) is True  # 续期不报错
        h2 = queue._hold_handles["e2"]
        assert h1 is not h2 and h1.cancelled()
        assert queue.find("e2").held is True
        queue.destroy()
        assert not queue._hold_handles and h2.cancelled()

    asyncio.run(_run())


def test_hold_default_is_permanent():
    """缺省不传 ttl_ms：永久阻塞，不挂自动放行定时器，仅 release 解除。"""

    async def _run():
        queue, rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        assert queue.hold("e2", reason="manual") is True
        assert "e2" not in queue._hold_handles
        queue.complete("e1")
        await asyncio.sleep(0.05)
        # 队头被永久 hold 挡住：不投递、不放行
        assert rec.delivered == ["e1"]
        assert queue.is_queued("e2")
        assert queue.release("e2") is True
        await asyncio.sleep(0.05)
        assert rec.delivered == ["e1", "e2"]

    asyncio.run(_run())


def test_hold_exempts_queue_timeout_and_release_rearms():
    async def _run():
        queue, rec = _make_queue(queue_timeout_ms=50)
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        assert queue.hold("e2", reason="editing") is True
        await asyncio.sleep(0.12)
        # 排队超时被豁免：e2 仍在队列里
        assert queue.is_queued("e2")
        assert not any(state == STATE_FAILED for _, state, _ in rec.states)
        assert queue.release("e2") is True
        await asyncio.sleep(0.12)
        return queue, rec

    queue, rec = asyncio.run(_run())
    # release 重挂超时：到期后按老语义 fail
    assert ("e2", STATE_FAILED, {"reason": "queue timeout"}) in rec.states
    assert not queue.is_queued("e2")


def test_cancel_held_item_clears_hold_timer_and_queue_flows():
    async def _run():
        queue, rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        queue.submit(_item("e3"))
        assert queue.hold("e2", reason="manual", ttl_ms=60_000) is True
        handle = queue._hold_handles["e2"]
        assert queue.cancel_queued("e2") is True
        assert "e2" not in queue._hold_handles and handle.cancelled()
        queue.complete("e1")
        for _ in range(4):
            await asyncio.sleep(0)
        return queue, rec

    queue, rec = asyncio.run(_run())
    assert ("e2", STATE_CANCELED, {"reason": "canceled by user"}) in rec.states
    # held 项被取消后队列继续流转
    assert rec.delivered == ["e1", "e3"]


def test_clear_and_drain_all_clear_hold_timers():
    async def _run():
        queue, _rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        queue.submit(_item("e3"))
        assert queue.hold("e2", reason="manual", ttl_ms=60_000) is True
        assert queue.hold("e3", reason="editing", ttl_ms=60_000) is True
        h2 = queue._hold_handles["e2"]
        h3 = queue._hold_handles["e3"]
        queue.clear("s1", "")
        assert not queue._hold_handles
        assert h2.cancelled() and h3.cancelled()

    asyncio.run(_run())


def test_drain_all_queued_clears_hold_timers():
    async def _run():
        queue, _rec = _make_queue()
        queue.submit(_item("e1"))
        queue.submit(_item("e2"))
        assert queue.hold("e2", reason="manual", ttl_ms=60_000) is True
        handle = queue._hold_handles["e2"]
        drained = queue.drain_all_queued()
        assert [item.event_id for item in drained] == ["e2"]
        assert not queue._hold_handles and handle.cancelled()

    asyncio.run(_run())
