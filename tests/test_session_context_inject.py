"""Unit tests for the one-shot [system-context] session_id injection."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from grix_hermes.adapter import (
    GrixAdapter,
    _is_new_hermes_session,
    _render_session_context_block,
)


class _FakeSessionStore:
    """Mimics the core SessionStore.get_or_create_session semantics."""

    def __init__(self):
        self.entry = None
        self._counter = 0

    def _new_entry(self, now, was_auto_reset):
        self._counter += 1
        return SimpleNamespace(
            session_id=f"hermes-sess-{self._counter}",
            created_at=now,
            updated_at=now,
            was_auto_reset=was_auto_reset,
        )

    def get_or_create_session(self, source):
        now = datetime.now()
        if self.entry is None:
            self.entry = self._new_entry(now, False)
        elif getattr(self.entry, "expired", False):
            self.entry = self._new_entry(now, True)
        else:
            self.entry.updated_at = now + timedelta(microseconds=1)
        return self.entry


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


def test_is_new_hermes_session():
    now = datetime.now()
    assert _is_new_hermes_session(SimpleNamespace(created_at=now, updated_at=now))
    assert not _is_new_hermes_session(
        SimpleNamespace(created_at=now, updated_at=now + timedelta(seconds=1))
    )
    assert _is_new_hermes_session(
        SimpleNamespace(created_at=now, updated_at=now + timedelta(seconds=1), was_auto_reset=True)
    )


def test_new_session_injects_once():
    adapter = _adapter_stub(_FakeSessionStore())
    block = _inject(adapter)
    assert 'Your current Grix session_id is "sess-1".' in block
    # Same session, second message: no re-injection.
    assert _inject(adapter) == ""


def test_existing_session_not_injected():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    store.get_or_create_session(object())  # create
    store.get_or_create_session(object())  # touch: created_at != updated_at
    assert _inject(adapter) == ""


def test_auto_reset_reinjects():
    store = _FakeSessionStore()
    adapter = _adapter_stub(store)
    assert _inject(adapter) != ""
    # idle/daily expiry -> next get_or_create auto-resets into a new session
    store.entry.expired = True
    block = _inject(adapter)
    assert 'Your current Grix session_id is "sess-1".' in block
    assert _inject(adapter) == ""


def test_same_hermes_session_race_not_reinjected():
    # Two dispatches before anything touches updated_at (entry still reads
    # created_at == updated_at): the injected session_id guard dedups.
    store = _FakeSessionStore()
    entry = store.get_or_create_session(object())
    store.get_or_create_session = lambda source: entry
    adapter = _adapter_stub(store)
    assert _inject(adapter) != ""
    assert _inject(adapter) == ""


def test_no_store_or_empty_session_id_skips():
    adapter = _adapter_stub(None)
    assert _inject(adapter) == ""
    adapter = _adapter_stub(_FakeSessionStore())
    assert _inject(adapter, session_id="") == ""
