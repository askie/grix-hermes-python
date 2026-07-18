"""进程内事件队列：排队、调度、取消、重排、快照。

对齐 grix-connector 的 ``src/bridge/event-queue.ts``（EventQueue 类）语义：

- 每个并发组（``group_key``）同时只执行一个事件，其余按到达顺序排队；
- 排队/运行/终态变化通过 ``on_state_change`` 上报（queued 带位置与总数）；
- 支持精确取消单个排队事件、静默摘除、按会话清空、按会话重排（愿望清单
  语义，绝不报错）、按会话快照；
- 队列满拒绝新事件（上报 failed），可选排队超时；
- 出队闸门用"暂停原因集合"，多个暂停源可叠加互不踩踏。

队列为纯内存态，进程重启即丢失（与 connector 一致）。本模块不做任何
I/O——状态上报与事件投递全部通过回调交给使用方。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# submit() 结果
SUBMIT_RUNNING = "running"
SUBMIT_QUEUED = "queued"
SUBMIT_REJECTED = "rejected"

# on_state_change 的 state 取值（与协议 event_state 一致）
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_CANCELED = "canceled"
STATE_FAILED = "failed"

_PREVIEW_MAX_CHARS = 64


def build_preview(text: Optional[str]) -> str:
    """压缩空白并截断，生成队列项的内容预览（对齐 connector buildQueueItemTitle）。"""
    collapsed = " ".join(str(text or "").split())
    if not collapsed:
        return "Message"
    if len(collapsed) > _PREVIEW_MAX_CHARS:
        return f"{collapsed[:_PREVIEW_MAX_CHARS]}..."
    return collapsed


@dataclass
class QueueItem:
    """一条待执行/执行中的事件。

    ``session_id``/``owner_key`` 是协议维度（快照、清空、重排按它们过滤），
    ``group_key`` 是并发维度（同组串行），``payload`` 是投递时原样带回的
    不透明载荷，队列自身不解释。
    """

    event_id: str
    session_id: str
    group_key: str
    owner_key: str = ""
    preview: str = ""
    payload: Any = None


@dataclass
class EventQueueConfig:
    max_queued: int = 5
    queue_timeout_ms: int = 0
    cancelable_queued: bool = True
    cancelable_running: bool = True


class EventQueue:
    """事件排队与调度。回调均为同步函数，由使用方自行调度异步工作。

    - ``on_deliver(item)``：事件获得执行槽位，应开始执行；
    - ``on_state_change(item, state, meta)``：状态变化（queued/running/
      canceled/failed），meta 含 queue_position/queue_total/reason。
      终态（canceled/failed）表示事件从未进入执行，由使用方负责发终态回执。
    """

    def __init__(
        self,
        config: EventQueueConfig,
        *,
        on_deliver: Callable[[QueueItem], None],
        on_state_change: Callable[[QueueItem, str, Dict[str, Any]], None],
    ) -> None:
        self._config = config
        self._on_deliver = on_deliver
        self._on_state_change = on_state_change
        self._running: Dict[str, QueueItem] = {}
        self._queued: List[QueueItem] = []
        self._timeout_handles: Dict[str, asyncio.TimerHandle] = {}
        self._pause_reasons: set[str] = set()
        self._drain_scheduled = False

    # ── 查询 ──────────────────────────────────────────────────────────

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._queued)

    def is_running(self, event_id: str) -> bool:
        return event_id in self._running

    def is_queued(self, event_id: str) -> bool:
        return any(item.event_id == event_id for item in self._queued)

    def find(self, event_id: str) -> Optional[QueueItem]:
        item = self._running.get(event_id)
        if item is not None:
            return item
        for queued in self._queued:
            if queued.event_id == event_id:
                return queued
        return None

    def group_busy(self, group_key: str) -> bool:
        return any(item.group_key == group_key for item in self._running.values())

    def has_queued_for_group(self, group_key: str) -> bool:
        return any(item.group_key == group_key for item in self._queued)

    def snapshot(self, session_id: str, owner_key: str = "") -> Dict[str, Any]:
        """按会话取快照：running / running_items / queued（含 1 起位置）。"""

        def _matches(item: QueueItem) -> bool:
            return item.session_id == session_id and item.owner_key == owner_key

        running_items = [item for item in self._running.values() if _matches(item)]
        queued_items = [item for item in self._queued if _matches(item)]
        return {
            "running": [item.event_id for item in running_items],
            "running_items": [
                {"event_id": item.event_id, "content_preview": item.preview}
                for item in running_items
            ],
            "queued": [
                {
                    "event_id": item.event_id,
                    "position": position,
                    "content_preview": item.preview,
                }
                for position, item in enumerate(queued_items, start=1)
            ],
        }

    # ── 出队闸门 ──────────────────────────────────────────────────────

    @property
    def _ready(self) -> bool:
        return not self._pause_reasons

    def pause(self, reason: str) -> None:
        """按原因暂停出队；同一原因重复调用幂等。"""
        self._pause_reasons.add(reason)

    def resume(self, reason: str) -> None:
        """解除某原因的暂停；所有原因解除后立即续投排队事件。"""
        if reason not in self._pause_reasons:
            return
        self._pause_reasons.discard(reason)
        if self._ready:
            self._drain()

    # ── 提交 / 完成 ───────────────────────────────────────────────────

    def submit(self, item: QueueItem) -> str:
        """提交事件。同组空闲立即投递；否则排队；组内队列满则拒绝（上报 failed）。"""
        if item.event_id in self._running or self.is_queued(item.event_id):
            # 上游已有事件级 dedup，这里兜底防重复占坑。
            return SUBMIT_QUEUED

        if self._ready and not self.group_busy(item.group_key):
            self._start(item)
            return SUBMIT_RUNNING

        group_depth = sum(1 for queued in self._queued if queued.group_key == item.group_key)
        if self._config.max_queued <= 0 or group_depth >= self._config.max_queued:
            self._on_state_change(item, STATE_FAILED, {"reason": "queue full"})
            return SUBMIT_REJECTED

        self._queued.append(item)
        self._notify_positions(item.session_id, item.owner_key)
        if self._config.queue_timeout_ms > 0:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                self._timeout_handles[item.event_id] = loop.call_later(
                    self._config.queue_timeout_ms / 1000,
                    self._timeout_event,
                    item.event_id,
                )
        return SUBMIT_QUEUED

    def complete(self, event_id: str) -> None:
        """事件到达终态，释放其执行槽位并调度续投。

        幂等：事件不在 running 中时只触发一次续投检查。续投延迟到下一个
        事件循环 tick——complete 通常在收口链路的同步栈里被调用，此刻
        使用方的轮次清理尚未完成，立刻投递下一条会踩到上一轮的残留状态
        （对齐 connector 的 queueMicrotask 延迟出队）。
        """
        self._running.pop(event_id, None)
        self._cancel_timeout(event_id)
        self._schedule_drain()

    # ── 取消 / 移除 / 清空 / 重排 ─────────────────────────────────────

    def cancel_queued(self, event_id: str, *, reason: str = "canceled by user") -> bool:
        """取消一个排队中的事件：摘除并上报 canceled 终态。运行中的不归这里管。"""
        if not self._config.cancelable_queued:
            return False
        item = self._take_queued(event_id)
        if item is None:
            return False
        self._on_state_change(item, STATE_CANCELED, {"reason": reason})
        self._notify_positions(item.session_id, item.owner_key)
        self._schedule_drain()
        return True

    def remove_queued(self, event_id: str) -> Optional[QueueItem]:
        """静默摘除一个排队中的事件：不上报终态，由调用方自行走停止协议。"""
        item = self._take_queued(event_id)
        if item is None:
            return None
        self._notify_positions(item.session_id, item.owner_key)
        self._schedule_drain()
        return item

    def clear(self, session_id: str, owner_key: str = "", *, reason: str = "queue cleared") -> List[QueueItem]:
        """清空某会话的全部排队事件，逐条上报 canceled 终态。不影响运行中的事件。"""
        cleared: List[QueueItem] = []
        remaining: List[QueueItem] = []
        for item in self._queued:
            if item.session_id == session_id and item.owner_key == owner_key:
                cleared.append(item)
            else:
                remaining.append(item)
        self._queued = remaining
        for item in cleared:
            self._cancel_timeout(item.event_id)
            self._on_state_change(item, STATE_CANCELED, {"reason": reason})
        if cleared:
            self._schedule_drain()
        return cleared

    def reorder(self, session_id: str, ordered_event_ids: List[str], owner_key: str = "") -> List[str]:
        """重排某会话的排队事件（愿望清单语义，绝不报错）。

        - 清单里已不在队列的 id 忽略（已出队/已取消）；
        - 队列里有、清单里没有的（重排请求飞行途中新入队的）按原相对顺序
          排在该会话段尾部；
        - 其他会话的排队事件占位不动（本会话事件只在自己原有槽位间重排）。
        返回应用后的实际排队顺序。
        """
        slots: List[int] = []
        session_items: List[QueueItem] = []
        for index, item in enumerate(self._queued):
            if item.session_id == session_id and item.owner_key == owner_key:
                slots.append(index)
                session_items.append(item)
        if not session_items:
            return []

        pending = {item.event_id: item for item in session_items}
        reordered: List[QueueItem] = []
        for event_id in ordered_event_ids:
            item = pending.pop(event_id, None)
            if item is not None:
                reordered.append(item)
        for item in session_items:
            if item.event_id in pending:
                reordered.append(item)

        changed = any(new is not old for new, old in zip(reordered, session_items))
        if changed:
            for slot, item in zip(slots, reordered):
                self._queued[slot] = item
            self._notify_positions(session_id, owner_key)
        return [item.event_id for item in reordered]

    def destroy(self) -> None:
        for handle in self._timeout_handles.values():
            handle.cancel()
        self._timeout_handles.clear()
        self._queued.clear()
        self._running.clear()
        self._pause_reasons.clear()

    # ── 内部 ──────────────────────────────────────────────────────────

    def _start(self, item: QueueItem) -> None:
        self._running[item.event_id] = item
        self._on_state_change(item, STATE_RUNNING, {})
        self._on_deliver(item)

    def _take_queued(self, event_id: str) -> Optional[QueueItem]:
        for index, item in enumerate(self._queued):
            if item.event_id == event_id:
                del self._queued[index]
                self._cancel_timeout(event_id)
                return item
        return None

    def _timeout_event(self, event_id: str) -> None:
        self._timeout_handles.pop(event_id, None)
        item = self._take_queued(event_id)
        if item is None:
            return
        self._on_state_change(item, STATE_FAILED, {"reason": "queue timeout"})
        self._notify_positions(item.session_id, item.owner_key)
        self._schedule_drain()

    def _cancel_timeout(self, event_id: str) -> None:
        handle = self._timeout_handles.pop(event_id, None)
        if handle is not None:
            handle.cancel()

    def _schedule_drain(self) -> None:
        if self._drain_scheduled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._drain()
            return
        self._drain_scheduled = True

        def _run() -> None:
            self._drain_scheduled = False
            self._drain()

        loop.call_soon(_run)

    def _drain(self) -> None:
        """扫描队列，投递所有"所在组已空闲"的队首事件。

        队头所在组仍忙时跳过继续向后扫——一个组的拥塞不阻塞其他组
        （connector 的队列按 slot 隔离天然如此，这里用全局列表 + 组内
        串行达到同样效果）。
        """
        if not self._ready:
            return
        busy_groups = {item.group_key for item in self._running.values()}
        index = 0
        while index < len(self._queued):
            item = self._queued[index]
            if item.group_key in busy_groups:
                index += 1
                continue
            del self._queued[index]
            self._cancel_timeout(item.event_id)
            busy_groups.add(item.group_key)
            self._start(item)
            self._notify_positions(item.session_id, item.owner_key)

    def _notify_positions(self, session_id: str, owner_key: str) -> None:
        """向某会话的全部排队事件广播当前位置（1 起）与总数。"""
        session_items = [
            item
            for item in self._queued
            if item.session_id == session_id and item.owner_key == owner_key
        ]
        total = len(session_items)
        for position, item in enumerate(session_items, start=1):
            self._on_state_change(
                item,
                STATE_QUEUED,
                {"queue_position": position, "queue_total": total},
            )
