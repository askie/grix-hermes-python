import asyncio
import sys
import types
from collections import defaultdict
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    LOCAL_ACTION_CONFIGURE_GATEWAY_PROVIDER,
    LOCAL_ACTION_SET_MODEL,
    STATUS_FAILED,
    STATUS_OK,
)


def _adapter():
    adapter = object.__new__(GrixAdapter)
    adapter.name = "grix-test"
    adapter._client = SimpleNamespace(
        send_local_action_result=AsyncMock(),
        send_local_action_result_confirmed=AsyncMock(return_value=True),
    )
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


def test_handle_set_model_dispatches_hermes_model_command():
    adapter = _adapter()
    adapter._message_handler = AsyncMock(return_value="switched")
    adapter._push_queue_snapshot = AsyncMock()
    adapter._toolbar_available_models = [
        {
            "id": "deepseek-v4-pro",
            "displayName": "DeepSeek Pro",
            "provider": "opencode-go",
        }
    ]
    source = SimpleNamespace(chat_id="s1", chat_type="dm", thread_id=None)

    payload = {
        "action_id": "act-model",
        "action_type": LOCAL_ACTION_SET_MODEL,
        "params": {
            "session_id": "s1",
            "model_id": "deepseek-v4-pro",
            "display_label": "DeepSeek Pro",
        },
    }

    with _packet_ctx(adapter):
        adapter._active_state().latest_sources["s1"] = source
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    event = adapter._message_handler.await_args.args[0]
    assert event.text == "/model deepseek-v4-pro --provider opencode-go"
    assert event.source is source
    adapter._client.send_local_action_result.assert_awaited_once_with(
        action_id="act-model",
        status=STATUS_OK,
        result={
            "session_id": "s1",
            "model_id": "deepseek-v4-pro",
            "provider": "opencode-go",
            "display_label": "DeepSeek Pro",
        },
    )
    adapter._push_queue_snapshot.assert_awaited_once_with("s1", "")


def test_handle_set_model_rolls_back_relay_when_hermes_command_fails():
    adapter = _adapter()
    adapter._resolve_hermes_home = lambda: "/tmp/grix-hermes-profile"
    adapter._message_handler = AsyncMock(side_effect=RuntimeError("switch rejected"))
    adapter._toolbar_available_models = [{"id": "deepseek-v4-flash", "provider": "grix"}]
    source = SimpleNamespace(chat_id="s1", chat_type="dm", thread_id=None)
    snapshot = object()
    payload = {
        "action_id": "act-model-relay",
        "action_type": LOCAL_ACTION_SET_MODEL,
        "params": {
            "session_id": "s1",
            "model_id": "deepseek-v4-flash",
            "openai_base_url": "https://relay.example/openai",
            "api_key": "secret-relay-key",
        },
    }

    with _packet_ctx(adapter):
        adapter._active_state().latest_sources["s1"] = source
        with patch(
            "grix_hermes.adapter._configure_relay_for_model_switch",
            return_value=({"relay": "enabled"}, snapshot),
        ) as configure, patch(
            "grix_hermes.relay_credentials.restore_relay_configuration"
        ) as restore:
            asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    configure.assert_called_once()
    restore.assert_called_once_with("/tmp/grix-hermes-profile", snapshot)
    result = adapter._client.send_local_action_result.await_args
    assert result.kwargs["status"] == STATUS_FAILED
    assert result.kwargs["error_code"] == "model_switch_failed"
    assert "secret-relay-key" not in str(result)


def test_configure_gateway_provider_writes_standalone_hermes_profile():
    adapter = _adapter()
    adapter._resolve_hermes_home = lambda: "/tmp/grix-hermes-profile"
    adapter._schedule_gateway_restart_after_relay_change = Mock()
    payload = {
        "action_id": "act-relay",
        "action_type": LOCAL_ACTION_CONFIGURE_GATEWAY_PROVIDER,
        "params": {
            "openai_base_url": "https://relay.example/openai",
            "api_key": "secret-relay-key",
            "model": "deepseek-v4-flash",
        },
    }

    with _packet_ctx(adapter):
        with patch(
            "grix_hermes.relay_credentials.configure_relay_credentials_with_snapshot",
            return_value=(
                {"relay": "enabled", "model": "deepseek-v4-flash", "restart_required": True},
                Mock(name="relay_snapshot"),
            ),
        ) as configure:
            asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))

    configure.assert_called_once()
    assert configure.call_args.args[0] == "/tmp/grix-hermes-profile"
    assert configure.call_args.kwargs["credentials"].model == "deepseek-v4-flash"
    adapter._client.send_local_action_result_confirmed.assert_awaited_once_with(
        action_id="act-relay",
        status=STATUS_OK,
        result={"relay": "enabled", "model": "deepseek-v4-flash", "restart_required": True},
    )
    adapter._schedule_gateway_restart_after_relay_change.assert_called_once_with()
    assert "secret-relay-key" not in str(adapter._client.send_local_action_result_confirmed.await_args)
