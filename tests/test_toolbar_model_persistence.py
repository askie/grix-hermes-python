import asyncio
import json
import sys
import types
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

_tools_stub = sys.modules.setdefault("tools", types.ModuleType("tools"))
if "tools.approval" not in sys.modules:
    _approval_stub = types.ModuleType("tools.approval")
    _approval_stub.resolve_gateway_approval = lambda *a, **k: 0
    sys.modules["tools.approval"] = _approval_stub
    _tools_stub.approval = _approval_stub

from grix_hermes.adapter import GrixAdapter, _CURRENT_CLIENT_CTX, _OwnerState
from grix_hermes.contract import LOCAL_ACTION_SET_MODEL, STATUS_OK
from grix_hermes.persistence.toolbar_model_store import ToolbarModelStore


def _models():
    return [
        {"id": "m-a", "displayName": "A", "provider": "p1", "providerLabel": "P1"},
        {"id": "m-b", "displayName": "B", "provider": "p2", "providerLabel": "P2"},
    ]


def _adapter(store_path=None):
    adapter = object.__new__(GrixAdapter)
    adapter.name = "grix-test"
    adapter._client = SimpleNamespace(
        send_local_action_result=AsyncMock(),
        send_local_action_result_confirmed=AsyncMock(return_value=True),
    )
    adapter._owner_states = defaultdict(_OwnerState)
    adapter._toolbar_model_id = "m-a"
    adapter._toolbar_model_provider = "p1"
    adapter._toolbar_available_models = _models()
    adapter._toolbar_model_store = ToolbarModelStore(store_path)
    adapter._toolbar_session_models = adapter._toolbar_model_store.sessions
    adapter._message_handler = AsyncMock()
    adapter._resolve_hermes_home = lambda: "/nonexistent-hermes-home"
    adapter._push_queue_snapshot = AsyncMock()
    adapter.build_source = lambda chat_id, chat_type: SimpleNamespace(chat_id=chat_id, chat_type=chat_type)
    adapter._provider_quota_toolbar_meta = lambda: {}
    adapter._session_store = None
    return adapter


def _run_set_model(adapter, session_id, model_id, provider):
    payload = {
        "action_id": "act-1",
        "action_type": LOCAL_ACTION_SET_MODEL,
        "params": {"session_id": session_id, "model_id": model_id, "provider": provider},
    }
    token = _CURRENT_CLIENT_CTX.set(adapter._client)
    try:
        asyncio.run(GrixAdapter._handle_local_action_packet(adapter, payload))
    finally:
        _CURRENT_CLIENT_CTX.reset(token)


def test_store_roundtrip(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    store = ToolbarModelStore(path)
    store.set_session("o\0s1", {"model_id": "m-b", "provider": "p2", "display_label": "B"})
    store.set_session("o\0s2", {"model_id": "m-a", "provider": "p1"}, update_global=False)
    data = json.loads(open(path).read())
    assert data["global"] == {"": {"model_id": "m-b", "provider": "p2", "display_label": "B"}}
    reloaded = ToolbarModelStore(path)
    assert reloaded.get_session("o\0s1")["model_id"] == "m-b"
    assert reloaded.get_session("o\0s2")["provider"] == "p1"
    assert reloaded.get_global()["model_id"] == "m-b"
    assert reloaded.get_global("other-owner") is None


def test_store_global_is_per_owner_and_sessions_are_bounded(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    store = ToolbarModelStore(path, max_sessions=2)
    store.set_session("o\0s1", {"model_id": "m-a"}, owner_key="")
    store.set_session("u\0s2", {"model_id": "m-b"}, owner_key="u")
    store.set_session("o\0s3", {"model_id": "m-a"}, owner_key="", update_global=False)
    assert store.get_global("")["model_id"] == "m-a"
    assert store.get_global("u")["model_id"] == "m-b"
    assert list(store.sessions) == ["u\0s2", "o\0s3"]
    reloaded = ToolbarModelStore(path)
    assert list(reloaded.sessions) == ["u\0s2", "o\0s3"]
    assert reloaded.get_global("u")["model_id"] == "m-b"


def test_store_reads_legacy_flat_global(tmp_path):
    path = tmp_path / "toolbar-models.json"
    path.write_text(json.dumps({"sessions": {}, "global": {"model_id": "m-b", "provider": "p2"}}))
    assert ToolbarModelStore(str(path)).get_global("")["model_id"] == "m-b"


def test_store_ignores_broken_entries(tmp_path):
    path = tmp_path / "toolbar-models.json"
    path.write_text(json.dumps({"sessions": {"k": {"provider": "p"}, "": {"model_id": "x"}}, "global": "bad"}))
    store = ToolbarModelStore(str(path))
    assert store.sessions == {}
    assert store.get_global() is None


def test_set_model_persists_session_and_global(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    adapter = _adapter(path)
    _run_set_model(adapter, "sess-1", "m-b", "p2")
    adapter._client.send_local_action_result.assert_awaited_once()
    assert adapter._client.send_local_action_result.await_args.kwargs["status"] == STATUS_OK
    reloaded = ToolbarModelStore(path)
    assert reloaded.get_session("\0sess-1")["model_id"] == "m-b"
    assert reloaded.get_global() == {"model_id": "m-b", "provider": "p2", "display_label": "B"}


def test_new_session_resolves_global_default(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0old", {"model_id": "m-b", "provider": "p2", "display_label": "B"})
    adapter = _adapter(path)
    # restart: session entry survives, unseen session falls back to global
    assert GrixAdapter._toolbar_effective_model(adapter, "", "old")["model_id"] == "m-b"
    assert GrixAdapter._toolbar_effective_model(adapter, "", "fresh")["provider"] == "p2"
    adapter._toolbar_model_store = ToolbarModelStore(None)
    adapter._toolbar_session_models = {}
    assert GrixAdapter._toolbar_effective_model(adapter, "", "fresh") == {}


def test_inherit_sends_model_command_for_new_session(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0old", {"model_id": "m-b", "provider": "p2", "display_label": "B"})
    adapter = _adapter(path)
    source = SimpleNamespace(chat_id="fresh")
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "fresh", "", source, "k", fresh=True))
    adapter._message_handler.assert_awaited_once()
    event = adapter._message_handler.await_args.args[0]
    assert event.text == "/model m-b --provider p2"
    assert event.raw_message["_grix_kind"] == "toolbar_model_inherit"
    # session entry recorded without touching global
    reloaded = ToolbarModelStore(path)
    assert reloaded.get_session("\0fresh")["model_id"] == "m-b"
    assert reloaded.get_global()["model_id"] == "m-b"


def test_inherit_skips_when_matching_config_default(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0old", {"model_id": "m-a", "provider": "p1"})
    adapter = _adapter(path)
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "fresh", "", SimpleNamespace(), "k", fresh=True))
    adapter._message_handler.assert_not_awaited()
    assert ToolbarModelStore(path).get_session("\0fresh") is None


def test_inherit_skips_model_missing_from_catalog(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0old", {"model_id": "gone", "provider": "p9"})
    adapter = _adapter(path)
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "fresh", "", SimpleNamespace(), "k", fresh=True))
    adapter._message_handler.assert_not_awaited()
    assert ToolbarModelStore(path).get_session("\0fresh") is None


def test_inherit_failure_does_not_record_session(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0old", {"model_id": "m-b", "provider": "p2"})
    adapter = _adapter(path)
    adapter._message_handler = AsyncMock(side_effect=RuntimeError("boom"))
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "fresh", "", SimpleNamespace(), "k", fresh=True))
    assert ToolbarModelStore(path).get_session("\0fresh") is None


class _FakeStore:
    """核心 SessionStore 的最小桩：entries / reset 判定 / 覆盖持久化。"""

    def __init__(self, entry=None, reset_pending=False):
        self._entries = {"core-k": entry} if entry is not None else {}
        self._reset_pending = reset_pending
        self.overrides = {}
        self.calls = []

    def _generate_session_key(self, source):
        return "core-k"

    def _should_reset(self, entry, source):
        return self._reset_pending

    def get_or_create_session(self, source, force_new=False, touch_activity=True):
        self.calls.append(("get_or_create", touch_activity))
        self._reset_pending = False
        self._entries["core-k"] = SimpleNamespace(session_id="new", was_auto_reset=True)
        return self._entries["core-k"]

    def set_model_override(self, key, override):
        self.calls.append(("set", key, override))
        self.overrides[key] = override

    def get_model_override(self, key):
        return self.overrides.get(key)


def test_reset_pending_persists_override_instead_of_command(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0old", {"model_id": "m-b", "provider": "p2"})
    adapter = _adapter(path)
    adapter._session_store = _FakeStore(entry=SimpleNamespace(session_id="old"), reset_pending=True)
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "fresh", "", SimpleNamespace(), "k", fresh=True))
    adapter._message_handler.assert_not_awaited()
    assert adapter._session_store.calls == [
        ("get_or_create", False),
        ("set", "core-k", {"model": "m-b", "provider": "p2"}),
    ]
    assert ToolbarModelStore(path).get_session("\0fresh")["model_id"] == "m-b"


def test_existing_session_backfills_missing_persisted_override(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0s", {"model_id": "m-b", "provider": "p2"})
    adapter = _adapter(path)
    adapter._session_store = _FakeStore(entry=SimpleNamespace(session_id="s"))
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "s", "", SimpleNamespace(), "k", fresh=False))
    adapter._message_handler.assert_not_awaited()
    assert adapter._session_store.overrides["core-k"] == {"model": "m-b", "provider": "p2"}
    # already persisted: no further writes
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "s", "", SimpleNamespace(), "k", fresh=False))
    assert len([c for c in adapter._session_store.calls if c[0] == "set"]) == 1


def test_existing_session_adopts_core_override_into_toolbar(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0s", {"model_id": "m-b", "provider": "p2"})
    adapter = _adapter(path)
    store = _FakeStore(entry=SimpleNamespace(session_id="s"))
    store.overrides["core-k"] = {"model": "m-a", "provider": "p1"}
    adapter._session_store = store
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "s", "", SimpleNamespace(), "k", fresh=False))
    assert not [c for c in store.calls if c[0] == "set"]
    assert ToolbarModelStore(path).get_session("\0s")["model_id"] == "m-a"
    assert ToolbarModelStore(path).get_global("")["model_id"] == "m-b"


def test_non_fresh_without_core_entry_never_sends_model_command(tmp_path):
    path = str(tmp_path / "toolbar-models.json")
    ToolbarModelStore(path).set_session("\0s", {"model_id": "m-b", "provider": "p2"})
    adapter = _adapter(path)
    adapter._session_store = None
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "s", "", SimpleNamespace(), "k", fresh=False))
    adapter._session_store = _FakeStore()  # store present, entry missing
    asyncio.run(GrixAdapter._sync_toolbar_model_for_turn(adapter, "s", "", SimpleNamespace(), "k", fresh=False))
    adapter._message_handler.assert_not_awaited()
