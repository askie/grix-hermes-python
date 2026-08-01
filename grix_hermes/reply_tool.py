"""Grix final-reply tool — deliver the task's final conclusion as a quoted message.

对齐 grix-connector 的 grix_reply 语义：过程中的流式文本一律不带引用（服务端把
「agent 引用另一 agent 的消息」视为隐式 @ 并触发对方接活，过程消息带引用会反复
误触发）；最终结论由模型显式调用本工具发送，自动引用触发消息——引用恰好在任务
完成那一刻出现一次，作为完成信号交棒给下一个 agent。
"""

import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

GRIX_REPLY_SCHEMA = {
    "name": "grix_reply",
    "description": (
        "Send your final reply — the conclusion the user is waiting for — for the "
        "current task. The message automatically quotes the message that triggered "
        "this task; that quote is the completion signal (in multi-agent pipelines it "
        "hands the work to the next agent), so call this exactly once, when the task "
        "is truly complete. Use normal streamed text for progress only; do not send "
        "the complete conclusion as plain text before or after calling this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The final reply text (the complete conclusion).",
            },
            "session_id": {
                "type": "string",
                "description": (
                    "Target session ID. Only needed when multiple tasks are running "
                    "concurrently; otherwise the current task's session is used."
                ),
            },
            "quoted_message_id": {
                "type": "string",
                "description": (
                    "Message ID to quote. Defaults to the message that triggered the "
                    "current task; override only to quote a different message."
                ),
            },
        },
        "required": ["text"],
    },
}


def _get_grix_adapter():
    from gateway.run import _gateway_runner_ref
    from gateway.config import Platform

    runner = _gateway_runner_ref()
    if not runner:
        return None
    return runner.adapters.get(Platform("grix"))


def _check_reply() -> bool:
    try:
        adapter = _get_grix_adapter()
        if not adapter:
            return False
        return bool(adapter.connection.endpoint and adapter.connection.api_key)
    except Exception:
        return False


def _collect_reply_targets(adapter) -> Dict[str, Dict[str, Any]]:
    """跨所有 owner 分桶收集正在处理中的应答目标（工具上下文可能拿不到
    packet ContextVar，不能只看 _active_state 的那一桶）。

    工具 handler 与网关主循环跑在不同线程，先快照再遍历，避免
    「dictionary changed size during iteration」竞态。"""
    targets: Dict[str, Dict[str, Any]] = {}
    for state in list(adapter._owner_states.values()):
        targets.update(dict(state.active_reply_targets))
    return targets


def _resolve_reply_target(
    targets: Dict[str, Dict[str, Any]],
    session_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """解析当前任务的应答目标，返回 (entry, error)。

    优先级：
    1. 任务链路 context 里的 session_key 精确匹配（并发任务下唯一可靠的归属）；
    2. 显式 session_id 按 chat_id 匹配（同群多个 per-user session 并发时取最新启动的）；
    3. 只有一个进行中任务时自动解析。
    """
    from .adapter import _CURRENT_REPLY_SESSION_KEY

    ctx_key = _CURRENT_REPLY_SESSION_KEY.get()
    if ctx_key and ctx_key in targets:
        entry = targets[ctx_key]
        if not session_id or str(entry.get("chat_id")) == session_id:
            return entry, None

    if session_id:
        matched = [t for t in targets.values() if str(t.get("chat_id")) == session_id]
        if not matched:
            return None, (
                f"no active task found for session {session_id}; "
                f"active sessions: {sorted({str(t.get('chat_id')) for t in targets.values()}) or 'none'}"
            )
        matched.sort(key=lambda t: t.get("started_at") or 0.0)
        return matched[-1], None
    if not targets:
        return None, "no active task run; grix_reply can only be used while handling a task"
    if len(targets) > 1:
        chat_ids = sorted({str(t.get("chat_id")) for t in targets.values()})
        return None, (
            "multiple tasks are running concurrently; pass session_id to pick one of: "
            + ", ".join(chat_ids)
        )
    return next(iter(targets.values())), None


async def _grix_reply_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result

    text = str(args.get("text") or "").strip()
    session_id = str(args.get("session_id") or "").strip()
    quoted_override = str(args.get("quoted_message_id") or "").strip()

    if not text:
        return tool_error("text is required")

    try:
        adapter = _get_grix_adapter()
        if not adapter:
            return tool_error("Grix adapter is not connected")

        entry, resolve_error = _resolve_reply_target(_collect_reply_targets(adapter), session_id)
        if resolve_error:
            return tool_error(resolve_error)

        # 重复调用保护：完成信号（引用）每轮只能出现一次，第二次起除非显式指定
        # quoted_message_id，否则不带引用发送，避免二次触发下一个 agent。
        already_replied = bool(entry.get("replied"))
        quoted_message_id = quoted_override or (
            None if already_replied else str(entry.get("message_id") or "").strip() or None
        )
        chat_id = str(entry.get("chat_id") or "").strip()
        source_client = entry.get("client") or adapter._client

        coro = adapter.send_final_reply(
            chat_id=chat_id,
            content=text,
            quoted_message_id=quoted_message_id,
            source_client=source_client,
        )
        # 工具 handler 运行在独立线程的事件循环上，而 transport 的请求/回包都绑定
        # 网关主循环——必须调度回主循环执行，否则跨 loop 唤醒存在竞态。
        main_loop = entry.get("loop")
        if main_loop is not None and main_loop.is_running() and main_loop is not asyncio.get_running_loop():
            result = await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(coro, main_loop)
            )
        else:
            result = await coro

        if not getattr(result, "success", False):
            return tool_error(f"final reply send failed: {getattr(result, 'error', 'unknown error')}")
        entry["replied"] = True
        payload: Dict[str, Any] = {
            "ok": True,
            "message_id": getattr(result, "message_id", None),
            "quoted_message_id": quoted_message_id,
        }
        if already_replied:
            payload["note"] = (
                "final reply was already sent for this task; this message was "
                "delivered without the completion quote"
            )
        return tool_result(payload)
    except Exception as exc:
        logger.warning("grix_reply failed: %s", exc, exc_info=True)
        safe_msg = str(exc)
        if len(safe_msg) > 200:
            safe_msg = safe_msg[:200] + "..."
        return tool_error(f"grix_reply failed: {safe_msg}")


def register_reply_tool(ctx=None) -> None:
    _register = ctx.register_tool if ctx else None
    kwargs = dict(
        name="grix_reply",
        toolset="grix",
        schema=GRIX_REPLY_SCHEMA,
        handler=_grix_reply_handler,
        check_fn=_check_reply,
        is_async=True,
        description=GRIX_REPLY_SCHEMA["description"],
        emoji="✅",
    )
    if _register:
        _register(**kwargs)
    else:
        from tools.registry import registry
        registry.register(**kwargs)
