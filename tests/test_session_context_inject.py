"""Unit tests for the one-shot [system-context] session_id injection."""

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

from grix_hermes.adapter import (
    GrixAdapter,
    _judge_session_context_inject,
    _render_session_context_block,
)
from grix_hermes.protocol import GrixInboundMessage


class _FakeSessionStore:
    """Read-only surface of the core SessionStore used by the injection judge."""

    def __init__(self):
        self._entries = {}
        self._counter = 0
        self.reset_reason = None  # what _should_reset returns

    def _ensure_loaded(self):
        pass

    def _generate_session_key(self, source):
        return "k1"

    def _should_reset(self, entry, source):
        return self.reset_reason

    def get_or_create_session(self, source):
        raise AssertionError("injection judge must not call get_or_create_session")

    def add_entry(self, *, fresh=True, was_auto_reset=False, suspended=False):
        now = datetime.now()
        self._counter += 1
        entry = SimpleNamespace(
            session_id=f"hermes-sess-{self._counter}",
            created_at=now,
            updated_at=now if fresh else now - timedelta(hours=1),
            was_auto_reset=was_auto_reset,
            suspended=suspended,
        )
        self._entries["k1"] = entry
        return entry


def _adapter_stub(store):
    return SimpleNamespace(
        name="grix",
        _session_store=store,
        _session_context_injected={},
    )


def _inject(adapter, session_id="sess-1", session_key="k1"):
    message = SimpleNamespace(session_id=session_id)
    return GrixAdapter._session_context_block_once(adapter, message, object(), session_key)


def test_render_block_text():
    block = _render_session_context_block("abc")
    assert block.startswith("[system-context]")
    assert 'Your current Grix session_id is "abc".' in block
    assert block.endswith("[/system-context]")


def test_brand_new_session_injects_without_creating_entry():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    block = _inject(adapter)
    assert 'Your current Grix session_id is "sess-1".' in block
    # Read-only judgment: no entry created, no mutating call.
    assert store._entries == {}


def test_second_message_same_session_skips():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    assert _inject(adapter) != ""
    # Core creates the entry inside handle_message (fresh: created_at == updated_at).
    entry = store.add_entry(fresh=True)
    assert _inject(adapter) == ""
    # The sentinel token binds to the materialized hermes session_id.
    assert adapter._session_context_injected["k1"] == entry.session_id


def test_ongoing_session_not_injected():
    store = _FakeSessionStore()
    store.add_entry(fresh=False)  # created_at != updated_at
    adapter = _adapter_stub(store)
    assert _inject(adapter) == ""


def test_race_before_core_creates_entry_injects_once():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    assert _inject(adapter) != ""
    # Second dispatch before the core created the entry: sentinel dedups.
    assert _inject(adapter) == ""


def test_stale_session_reinjects_around_auto_reset():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    assert _inject(adapter) != ""  # brand new, sentinel token ""
    entry = store.add_entry(fresh=True)
    assert _inject(adapter) == ""
    # Session ages past the idle policy; the core will auto-reset on this message.
    entry.updated_at = entry.updated_at - timedelta(hours=2)
    store.reset_reason = "idle"
    assert _inject(adapter) != ""
    # Core performs the reset: new entry, flag not yet consumed.
    reset_entry = store.add_entry(fresh=True, was_auto_reset=True)
    assert _inject(adapter) == ""
    # Core consumed the flag (run.py sets was_auto_reset = False).
    reset_entry.was_auto_reset = False
    assert _inject(adapter) == ""


def test_core_completed_auto_reset_reinjects():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    adapter._session_context_injected["k1"] = "previous-generation"
    # Reset happened outside a judged dispatch (e.g. record-only path); the
    # unconsumed was_auto_reset flag marks the fresh session.
    entry = store.add_entry(fresh=True, was_auto_reset=True)
    assert _inject(adapter) != ""
    assert adapter._session_context_injected["k1"] == entry.session_id
    entry.was_auto_reset = False
    assert _inject(adapter) == ""


def test_suspended_session_reinjects():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    adapter._session_context_injected["k1"] = "previous-generation"
    store.add_entry(fresh=False, suspended=True)
    assert _inject(adapter) != ""


def test_judge_skips_without_entries_dict():
    assert _judge_session_context_inject(object(), object(), "k1", None) == (False, None)


def test_no_store_or_empty_session_id_skips():
    adapter = _adapter_stub(None)
    assert _inject(adapter) == ""
    adapter = _adapter_stub(_FakeSessionStore())
    assert _inject(adapter, session_id="") == ""


def test_dispatch_puts_system_context_first(monkeypatch):
    # Other suites permanently stub gateway's MessageEvent with a bare type;
    # pin a kwargs-accepting fake here so the dispatch path is order-immune.
    class _FakeEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setattr("grix_hermes.adapter.MessageEvent", _FakeEvent)

    store = _FakeSessionStore()  # empty -> brand-new session -> inject
    captured = {}

    async def _handle_message(event):
        captured["text"] = event.text

    state = SimpleNamespace(session_open_event_ids={}, processing_message_ids={})
    adapter = SimpleNamespace(
        name="grix",
        _session_store=store,
        _session_context_injected={},
        _active_sessions=set(),
        _inflight_dispatch_event_ids={},
        _pending_messages={},
        _active_state=lambda: state,
        handle_message=_handle_message,
        _event_still_open=lambda session_key, event_id: False,
        _session_has_queued_work=lambda session_key: False,
    )
    adapter._session_context_block_once = (
        lambda message, source, session_key: GrixAdapter._session_context_block_once(
            adapter, message, source, session_key
        )
    )
    message = GrixInboundMessage(
        event_id="e1",
        session_id="sess-1",
        sender_id="1",
        sender_name="n",
        chat_type="group",
        text="hi",
        message_id="999",
        session_type=2,
        attachments=[],
        raw={"context_messages": [{"msg_id": "50", "sender_id": "789", "content": "背景一句"}]},
    )
    asyncio.run(GrixAdapter._dispatch_grix_event(adapter, message, object(), "k1"))
    text = captured["text"]
    assert text.startswith("[system-context]")
    # [system-context] first, quoted-context block next, user text last.
    assert text.index("[/system-context]") < text.index("[789]：背景一句")
    assert text.index("[789]：背景一句") < text.rindex("hi")
