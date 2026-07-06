import asyncio
import sys
import types
from collections import defaultdict
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# handler 内 `from tools.approval import resolve_gateway_approval`（host 侧模块）。
# 测试环境用 stub 承载该子模块，具体返回值由各用例 patch 覆写。
_tools_stub = sys.modules.setdefault("tools", types.ModuleType("tools"))
_approval_stub = types.ModuleType("tools.approval")
_approval_stub.resolve_gateway_approval = lambda *a, **k: 0
sys.modules["tools.approval"] = _approval_stub
_tools_stub.approval = _approval_stub

from grix_hermes.adapter import GrixAdapter, _CURRENT_CLIENT_CTX, _OwnerState
from grix_hermes.contract import (
    ERR_APPROVAL_NOT_FOUND,
    LOCAL_ACTION_EXEC_APPROVE,
    STATUS_FAILED,
    STATUS_OK,
)


def _adapter():
    adapter = object.__new__(GrixAdapter)
    adapter.name = "grix-test"
    adapter._client = SimpleNamespace(send_local_action_result=AsyncMock())
    # 审批状态已迁到 per-owner 的 _active_state().approval_state（不再是扁平字段）。
    adapter._owner_states = defaultdict(_OwnerState)
    adapter.resume_typing_for_chat = lambda chat_id: None
    return adapter


@contextmanager
def _packet_ctx(adapter):
    # 生产中 packet handler 运行于 _CURRENT_CLIENT_CTX 已设为来源 client 的上下文，
    # _active_client() / _active_state() 均据此解析。测试同样设置并复位，令 owner
    # 状态的预置与包处理落在同一 owner_key 上，与真实分发一致。
    token = _CURRENT_CLIENT_CTX.set(adapter._client)
    try:
        yield
    finally:
        _CURRENT_CLIENT_CTX.reset(token)


def test_handle_local_action_resolves_via_session_key_mapping():
    adapter = _adapter()
    adapter.resume_typing_for_chat = lambda chat_id: setattr(adapter, "_resumed_chat", chat_id)

    payload = {
        "action_id": "act-1",
        "action_type": LOCAL_ACTION_EXEC_APPROVE,
        "params": {"approval_id": "ap1", "decision": "allow-once"},
    }

    with _packet_ctx(adapter):
        adapter._active_state().approval_state["ap1"] = {"session_key": "sess-1", "chat_id": "chat-1"}
        with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
            asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    resolve.assert_called_once_with("sess-1", "once")
    assert getattr(adapter, "_resumed_chat", None) == "chat-1"
    adapter._client.send_local_action_result.assert_awaited_once_with(
        action_id="act-1",
        status=STATUS_OK,
        result="allow-once",
    )


def test_handle_local_action_fails_when_approval_mapping_missing():
    adapter = _adapter()
    payload = {
        "action_id": "act-2",
        "action_type": LOCAL_ACTION_EXEC_APPROVE,
        "params": {"approval_id": "missing", "decision": "allow-once"},
    }

    with _packet_ctx(adapter):
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    adapter._client.send_local_action_result.assert_awaited_once_with(
        action_id="act-2",
        status=STATUS_FAILED,
        error_code=ERR_APPROVAL_NOT_FOUND,
        error_message="unknown or expired approval id",
    )
