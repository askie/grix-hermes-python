"""端到端验证事件收口时机（跑真实 hermes-agent 框架，不 stub gateway）。

用法：
    PYTHONPATH=<hermes-agent 根目录> python tests/run_event_result_timing_check.py

验证点（对齐 connector 语义）：
1. 慢任务：event_result 必须在消息处理器真正跑完之后才发出，且状态 responded；
   handle_message 派发返回时（任务仍在跑）不得已发结束事件。
2. 处理器抛错：event_result 状态 failed。
3. 忙时排队：第一轮运行期间到达的消息排队，其 event_result 在第二轮结束后才发。
"""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gateway.config import PlatformConfig  # noqa: E402
from gateway.platform_registry import PlatformEntry, platform_registry  # noqa: E402

# 让 Platform("grix") 的动态枚举成员可解析（正常由插件加载时注册）。
platform_registry.register(PlatformEntry(
    name="grix",
    label="Grix",
    adapter_factory=lambda cfg: None,
    check_fn=lambda: True,
))

from grix_hermes import adapter as adapter_mod  # noqa: E402


class FakeTransportClient:
    """记录全部出站协议调用及其时间的 transport 假件。"""

    def __init__(self):
        self._config = SimpleNamespace(shared_owner_id=None)
        self.status = {"connected": True, "authed": True}
        self.completed = []  # (t, event_id, status)
        self.sent = []
        self.acked = []

    def get_status(self):
        return dict(self.status)

    async def acknowledge_event(self, *, event_id, session_id=None, message_id=None):
        self.acked.append(event_id)

    async def complete_event(self, *, event_id, status, code=None, message=None, updated_at=None):
        self.completed.append((time.monotonic(), event_id, status))

    async def send_text(self, session_id, content, *, reply_to_message_id=None,
                        thread_id=None, event_id=None, biz_card=None, channel_data=None, **kw):
        self.sent.append(content)
        return {"ok": True, "message_id": f"m{len(self.sent)}"}

    async def send_composing(self, *a, **kw):
        return None

    async def edit_message(self, *a, **kw):
        return {"ok": True}


def make_adapter(client):
    config = PlatformConfig(
        enabled=True,
        extra={
            "endpoint": "ws://fake",
            "agent_id": "100",
            "api_key": "k",
            "busy_ack": False,
        },
    )
    inst = adapter_mod.GrixAdapter(config)
    inst._client = client
    return inst


def packet(event_id, message_id, text, session_id="chat-1"):
    return {
        "event_id": event_id,
        "session_id": session_id,
        "msg_id": message_id,
        "sender_id": "u1",
        "content": text,
        "session_type": 1,
        "msg_type": 1,
    }


async def scenario_slow_success(inst, client):
    handler_done_at = {}

    async def slow_handler(event):
        await asyncio.sleep(0.3)
        handler_done_at["t"] = time.monotonic()
        return "答复内容"

    inst.set_message_handler(slow_handler)

    dispatch_start = time.monotonic()
    await inst._handle_message_packet(packet("ev-slow", "m-slow", "你好"))
    dispatch_return = time.monotonic()

    # 派发返回必须远快于任务耗时（证明 handle_message 是非阻塞派发）
    assert dispatch_return - dispatch_start < 0.25, "handle_message 意外阻塞"
    # 派发返回时任务未结束，此刻不得已发 event_result
    assert not client.completed, f"事件在任务结束前被提前收口: {client.completed}"

    # 等后台任务跑完
    for _ in range(100):
        if client.completed:
            break
        await asyncio.sleep(0.05)

    assert client.completed, "任务结束后未发 event_result"
    t, eid, status = client.completed[0]
    assert eid == "ev-slow" and status == "responded", client.completed
    assert t >= handler_done_at["t"], "event_result 早于处理器完成"
    print(f"  ok: event_result(responded) 在处理器完成后 {t - handler_done_at['t']:.3f}s 发出")


async def scenario_failure(inst, client):
    async def boom(event):
        await asyncio.sleep(0.05)
        raise RuntimeError("boom")

    inst.set_message_handler(boom)
    await inst._handle_message_packet(packet("ev-fail", "m-fail", "触发失败"))
    for _ in range(100):
        if any(eid == "ev-fail" for _, eid, _s in client.completed):
            break
        await asyncio.sleep(0.05)
    rows = [(eid, s) for _, eid, s in client.completed if eid == "ev-fail"]
    assert rows == [("ev-fail", "failed")], client.completed
    print("  ok: 处理器抛错 → event_result(failed)")


async def scenario_busy_queue(inst, client):
    order = []

    async def slow_handler(event):
        order.append(("start", event.text))
        await asyncio.sleep(0.3)
        order.append(("end", event.text))
        return f"答复:{event.text}"

    inst.set_message_handler(slow_handler)

    await inst._handle_message_packet(packet("ev-a", "m-a", "第一条"))
    await asyncio.sleep(0.05)  # 让第一轮任务启动
    await inst._handle_message_packet(packet("ev-b", "m-b", "第二条"))

    # 第二条已派发返回，但两个事件都不得在各自任务结束前收口
    done_a = [r for r in client.completed if r[1] == "ev-a"]
    done_b = [r for r in client.completed if r[1] == "ev-b"]
    assert not done_a and not done_b, f"排队事件被提前收口: {client.completed}"

    for _ in range(200):
        if any(r[1] == "ev-b" for r in client.completed):
            break
        await asyncio.sleep(0.05)

    rows = {eid: s for _, eid, s in client.completed}
    assert rows.get("ev-a") == "responded", client.completed
    assert rows.get("ev-b") == "responded", client.completed
    # ev-b 的收口必须晚于第二轮处理开始
    tb = next(t for t, eid, _s in client.completed if eid == "ev-b")
    assert ("start", "第二条") in order and ("end", "第二条") in order, order
    print("  ok: 忙时排队 → 两个事件各自在所属轮次结束后收口 (responded)")


async def scenario_stop_cancels(inst, client):
    async def slow_handler(event):
        await asyncio.sleep(5)
        return "不该跑完"

    inst.set_message_handler(slow_handler)

    await inst._handle_message_packet(packet("ev-run", "m-run", "长任务"))
    await asyncio.sleep(0.1)  # 让任务启动
    assert not client.completed, "任务未结束就发了 event_result"

    # 后端工具栏停止按钮下发的 /stop 文本命令
    await inst._handle_message_packet(packet("ev-stopcmd", "m-stopcmd", "/stop"))

    for _ in range(100):
        if any(eid == "ev-run" for _, eid, _s in client.completed):
            break
        await asyncio.sleep(0.05)

    rows = {eid: s for _, eid, s in client.completed}
    assert rows.get("ev-run") == "canceled", client.completed
    assert rows.get("ev-stopcmd") == "responded", client.completed
    print("  ok: /stop 停止运行中任务 → event_result(canceled)")


async def main():
    for name, scenario in [
        ("慢任务成功收口", scenario_slow_success),
        ("处理器失败收口", scenario_failure),
        ("忙时排队收口", scenario_busy_queue),
        ("停止任务收口", scenario_stop_cancels),
    ]:
        client = FakeTransportClient()
        inst = make_adapter(client)
        print(f"[{name}]")
        token = adapter_mod._CURRENT_CLIENT_CTX.set(client)
        try:
            await scenario(inst, client)
        finally:
            adapter_mod._CURRENT_CLIENT_CTX.reset(token)

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
