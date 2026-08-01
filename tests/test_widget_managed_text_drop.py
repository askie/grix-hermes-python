"""托管代答私聊 text_events=drop 行为测试。

后端对 widget 客服等私聊托管场景下发 connector.text_events=drop（外加 tool/thinking
drop）。验证 hermes send() 按调用入口（is_final_reply）判定：
- 纯文本过程/续写（is_final_reply=False）被丢弃，不投递给对端；
- grix_reply 正式应答（send_final_reply → is_final_reply=True）照常投递；
- 开放式 clarify 提问照常投递（是发给对端的正常消息，非过程文本）；
- 未下发该 hint 时纯文本正常投递（无回归）。

复用 test_final_reply_quote 的 stub host + 假 transport。
"""

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS_DIR))

import test_final_reply_quote as h  # noqa: E402  触发 stub 安装

adapter_mod = h.adapter_mod
FakeTransportClient = h.FakeTransportClient
_make_adapter = h._make_adapter
_resolve_target = h._resolve_target
_with_ctx = h._with_ctx

MANAGED = {"tool_events": "drop", "thinking_events": "drop", "text_events": "drop"}


def _set_hints(inst, chat_id, hints):
    inst._active_state().session_connector_hints[str(chat_id)] = hints


def _set_reply_target(inst, *, chat_id="chat-1", message_id="t1", replied=True):
    inst._active_state().active_reply_targets["session-1"] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "replied": replied,
    }


def test_managed_drops_plain_process_text(monkeypatch):
    """托管场景：纯文本过程/续写被丢弃，但对 agent 报成功。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)

    result = _with_ctx(
        client, inst.send("chat-1", "在的呢～您可以直接说下使用场景", reply_to="t1")
    )

    assert result.success is True
    assert client.sent == []


def test_managed_keeps_final_reply(monkeypatch):
    """托管场景：grix_reply 正式应答照常投递并带引用。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)

    result = _with_ctx(
        client,
        inst.send_final_reply(
            chat_id="chat-1",
            content="正式客服回复",
            quoted_message_id="t1",
            source_client=client,
        ),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["content"] == "正式客服回复"
    assert client.sent[0]["reply_to_message_id"] == "t1"


def test_managed_keeps_open_clarify(monkeypatch):
    """托管场景：开放式提问是发给对端的正常消息，照常投递。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)

    result = _with_ctx(
        client, inst.send_clarify("chat-1", "请问您要咨询什么？", None, "cl1", "k")
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert "请问您要咨询什么？" in client.sent[0]["content"]


def test_managed_final_reply_survives_status_lookalike(monkeypatch):
    """正式应答正文即使长得像状态行(⏳ Working…)，托管场景也原样投递、不被误判丢弃。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)

    lookalike = "⏳ Working 时段是 9:00-18:00，您方便时我随时对接。"
    result = _with_ctx(
        client,
        inst.send_final_reply(
            chat_id="chat-1",
            content=lookalike,
            quoted_message_id="t1",
            source_client=client,
        ),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert "9:00-18:00" in client.sent[0]["content"]
    assert client.sent[0]["reply_to_message_id"] == "t1"


def test_managed_delivers_framework_final_text(monkeypatch):
    """托管场景：模型未走 grix_reply 时，框架整轮最终应答（metadata notify=True）必须投递。

    复现线上问题：base.py "Sending response" 路径经普通 send() 投递最终应答，
    此前被 text_events=drop 静默吞掉，对端完全无响应。
    """
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)

    result = _with_ctx(
        client,
        inst.send("chat-1", "您好呀，有什么可以帮您的？", reply_to="t1",
                  metadata={"notify": True}),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["content"] == "您好呀，有什么可以帮您的？"


def test_managed_drops_framework_final_after_grix_reply(monkeypatch):
    """托管场景：grix_reply 已成功投递时，不再重复投递框架整轮最终文本。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)
    _set_reply_target(inst)

    result = _with_ctx(
        client,
        inst.send("chat-1", "已回复。总结：正式客服回复", reply_to="t1",
                  metadata={"notify": True}),
    )

    assert result.success is True
    assert client.sent == []


def test_no_hint_drops_framework_final_after_grix_reply(monkeypatch):
    """普通私聊：grix_reply 已成功投递时，同轮 framework final 也必须去重。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst)

    result = _with_ctx(
        client,
        inst.send("chat-1", "已回复。总结：正式答复", reply_to="t1",
                  metadata={"notify": True}),
    )

    assert result.success is True
    assert client.sent == []


def test_framework_final_for_new_message_is_not_suppressed(monkeypatch):
    """同一会话的新消息不能被上一条消息的 replied 状态误伤。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst, message_id="old-message")

    result = _with_ctx(
        client,
        inst.send("chat-1", "新一轮最终答复", reply_to="new-message",
                  metadata={"notify": True}),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["content"] == "新一轮最终答复"


def test_managed_framework_final_survives_status_lookalike(monkeypatch):
    """托管场景：框架最终应答即使长得像状态行，也按最终应答原样投递（跳过过程分类）。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_hints(inst, "chat-1", MANAGED)

    lookalike = "⏳ Working 时段是 9:00-18:00，您方便时我随时对接。"
    result = _with_ctx(
        client,
        inst.send("chat-1", lookalike, reply_to="t1", metadata={"notify": True}),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert "9:00-18:00" in client.sent[0]["content"]


def test_no_hint_delivers_plain_text(monkeypatch):
    """无托管 hint：纯文本进程消息正常投递（无回归）。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)

    result = _with_ctx(client, inst.send("chat-1", "普通进程消息", reply_to="t1"))

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["content"] == "普通进程消息"


# ── grix_reply 之后的同轮流式文本收口 ────────────────────────────────────────


def _with_reply_ctx(session_key, fn):
    """在处理任务 context（_CURRENT_REPLY_SESSION_KEY）内执行 fn。"""
    token = adapter_mod._CURRENT_REPLY_SESSION_KEY.set(session_key)
    try:
        return fn()
    finally:
        adapter_mod._CURRENT_REPLY_SESSION_KEY.reset(token)


def test_drops_streamed_text_after_grix_reply(monkeypatch):
    """复现线上重复：grix_reply 已投递后，同轮模型续写的纯文本（无 notify、
    无 reply_to，流式文本通道）必须被丢弃，否则对端收到第二条重复总结。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst)

    result = _with_reply_ctx(
        "session-1",
        lambda: _with_ctx(client, inst.send("chat-1", "发布完成：重复总结")),
    )

    assert result.success is True
    assert client.sent == []


def test_delivers_streamed_text_before_grix_reply(monkeypatch):
    """grix_reply 尚未投递（replied=False）时，同轮流式过程文本正常投递。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst, replied=False)

    result = _with_reply_ctx(
        "session-1",
        lambda: _with_ctx(client, inst.send("chat-1", "构建完成，正在上传")),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["content"] == "构建完成，正在上传"


def test_post_reply_drop_requires_processing_ctx(monkeypatch):
    """ContextVar 缺失（非处理任务链路）时宁可放过，不误伤其它来源的文本。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst)

    result = _with_ctx(client, inst.send("chat-1", "其它来源的文本"))

    assert result.success is True
    assert len(client.sent) == 1


def test_post_reply_drop_ignores_other_sessions(monkeypatch):
    """群聊 per-user 并发：别的 session 已 replied 不影响本轮文本投递。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst)  # session-1 已 replied
    inst._active_state().active_reply_targets["session-2"] = {
        "chat_id": "chat-1",
        "message_id": "t2",
        "replied": False,
    }

    result = _with_reply_ctx(
        "session-2",
        lambda: _with_ctx(client, inst.send("chat-1", "另一用户的过程文本")),
    )

    assert result.success is True
    assert len(client.sent) == 1


def test_second_final_reply_survives_post_reply_drop(monkeypatch):
    """显式第二次 grix_reply（is_final_reply=True）不受收口影响。"""
    monkeypatch.setattr(adapter_mod, "resolve_grix_target", _resolve_target)
    client = FakeTransportClient()
    inst = _make_adapter(client)
    _set_reply_target(inst)

    result = _with_reply_ctx(
        "session-1",
        lambda: _with_ctx(
            client,
            inst.send_final_reply(
                chat_id="chat-1",
                content="补充说明",
                quoted_message_id=None,
                source_client=client,
            ),
        ),
    )

    assert result.success is True
    assert len(client.sent) == 1
    assert client.sent[0]["content"] == "补充说明"
