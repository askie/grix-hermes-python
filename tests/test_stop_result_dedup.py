"""event_stop 幂等判据回归：首次停止必须发出 stop_result。

缺陷根子：_handle_stop_packet 曾用消息投递共用的 seen_event_ids 判"重复停止"，
而 event_stop 携带的 event_id 就是被停事件的 id——投递时必然已记录，于是
"停一个正在运行的事件"100% 被误判为重复，转去重放缓存结果；首次停止时缓存
为空又静默 return，event_stop_result 永不发送，服务端 run 生命周期不闭环，
前端停止按钮永久 loading（线上 2026-07-18 会话 e2c929c5 复现）。

修复后的幂等判据 = completed_stop_results 是否已有该事件的停止终态；
connector 参考实现对 stop 不做事件级去重，每次都回终态，此处对齐。

走 stub 模式（同 test_event_result_timing.py），不依赖 hermes-agent host。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_event_result_timing import (  # noqa: E402  (装好 stub 后再导入 adapter)
    FakeTransportClient,
    _make_adapter,
    _with_ctx,
)

SESSION_ID = "e2c929c5-54f9-4922-8f7c-5899e5041af4"
EVENT_ID = f"{SESSION_ID}:2030840865701756928:2043190131619266560:2078461822738366464"


class FakeStopClient(FakeTransportClient):
    """在 complete_event 假件之上补记 stop 协议两步。"""

    def __init__(self):
        super().__init__()
        self.stop_acks = []
        self.stop_results = []

    async def acknowledge_stop(self, *, event_id, stop_id, accepted):
        self.stop_acks.append({"event_id": event_id, "stop_id": stop_id, "accepted": accepted})

    async def complete_stop(self, *, event_id, stop_id, status, code=None, message=None):
        self.stop_results.append(
            {"event_id": event_id, "stop_id": stop_id, "status": status, "code": code}
        )


def _prepare_stop_adapter(client):
    """装一个能跑通 _handle_stop_packet 运行中分支的 adapter。"""
    inst = _make_adapter(client)
    running_payload = (
        SimpleNamespace(message_id="m-1"),
        SimpleNamespace(chat_id=SESSION_ID, chat_type="dm", user_id=None, thread_id=None),
        f"agent:main:grix:dm:{SESSION_ID}",
    )
    inst._event_queue = SimpleNamespace(
        remove_queued=lambda eid: None,
        is_running=lambda eid: True,
        find=lambda eid: SimpleNamespace(payload=running_payload),
    )
    inst.force_stopped = []

    async def _force_stop(source, session_key, *, reply_to=None):
        inst.force_stopped.append(session_key)
        return True

    inst._force_stop_session = _force_stop
    return inst


def _stop_payload(stop_id):
    return {
        "event_id": EVENT_ID,
        "session_id": SESSION_ID,
        "stop_id": stop_id,
        "reason": "owner_requested_stop",
        "trigger_msg_id": "2078461822738366464",
    }


def test_first_stop_of_delivered_event_sends_stop_result():
    """回归主案：事件投递时已进 seen_event_ids，首次停止仍必须回 ack + result(stopped)。"""
    client = FakeStopClient()
    inst = _prepare_stop_adapter(client)
    inst._remember_event_id(EVENT_ID)  # 模拟该事件此前正常投递过

    _with_ctx(client, inst._handle_stop_packet(_stop_payload("stop_1")))

    assert inst.force_stopped, "运行中的会话必须被打断"
    assert client.stop_acks == [{"event_id": EVENT_ID, "stop_id": "stop_1", "accepted": True}]
    assert client.stop_results == [
        {"event_id": EVENT_ID, "stop_id": "stop_1", "status": "stopped", "code": None}
    ]
    # 终态已入缓存，供真正的重复停止重放
    assert inst._active_state().completed_stop_results[EVENT_ID]["status"] == "stopped"


def test_second_stop_of_same_event_replays_cached_result():
    """真重复（该事件的停止已完成过）：重放缓存终态，带新 stop_id。"""
    client = FakeStopClient()
    inst = _prepare_stop_adapter(client)
    inst._remember_event_id(EVENT_ID)

    _with_ctx(client, inst._handle_stop_packet(_stop_payload("stop_1")))
    _with_ctx(client, inst._handle_stop_packet(_stop_payload("stop_2")))

    assert [r["stop_id"] for r in client.stop_results] == ["stop_1", "stop_2"]
    assert {r["status"] for r in client.stop_results} == {"stopped"}
    assert len(client.stop_acks) == 2


def test_replay_without_cached_result_falls_back_to_terminal_status():
    """防御：缓存被淘汰时重放也绝不静默，兜底回 already_finished。"""
    client = FakeStopClient()
    inst = _prepare_stop_adapter(client)

    _with_ctx(client, inst._replay_completed_stop(EVENT_ID, "stop_x"))

    assert client.stop_results == [
        {"event_id": EVENT_ID, "stop_id": "stop_x", "status": "already_finished", "code": None}
    ]
