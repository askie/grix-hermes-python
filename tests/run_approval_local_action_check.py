import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from grix_hermes.adapter import GrixAdapter
from grix_hermes.contract import (
    ERR_APPROVAL_NOT_FOUND,
    LOCAL_ACTION_EXEC_APPROVE,
    STATUS_FAILED,
    STATUS_OK,
)


def make_adapter():
    adapter = object.__new__(GrixAdapter)
    adapter._client = SimpleNamespace(send_local_action_result=AsyncMock())
    adapter._approval_state = {}
    adapter.resume_typing_for_chat = lambda chat_id: None
    return adapter


def main():
    adapter = make_adapter()
    resumed = {}
    adapter.resume_typing_for_chat = lambda chat_id: resumed.setdefault("chat", chat_id)
    adapter._approval_state["ap1"] = {"session_key": "sess-1", "chat_id": "chat-1"}
    payload = {
        "action_id": "act-1",
        "action_type": LOCAL_ACTION_EXEC_APPROVE,
        "params": {"approval_id": "ap1", "decision": "allow-once"},
    }
    with patch("tools.approval.resolve_gateway_approval", return_value=1) as resolve:
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))
    resolve.assert_called_once_with("sess-1", "once")
    assert resumed["chat"] == "chat-1"
    adapter._client.send_local_action_result.assert_awaited_once_with(
        action_id="act-1", status=STATUS_OK, result="allow-once"
    )

    adapter2 = make_adapter()
    payload2 = {
        "action_id": "act-2",
        "action_type": LOCAL_ACTION_EXEC_APPROVE,
        "params": {"approval_id": "missing", "decision": "allow-once"},
    }
    asyncio.run(GrixAdapter._handle_local_action_packet(adapter2, payload2))
    adapter2._client.send_local_action_result.assert_awaited_once_with(
        action_id="act-2",
        status=STATUS_FAILED,
        error_code=ERR_APPROVAL_NOT_FOUND,
        error_message="unknown or expired approval id",
    )
    print("ok")


if __name__ == "__main__":
    main()
