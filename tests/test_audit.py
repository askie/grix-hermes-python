from pathlib import Path
import hashlib

from grix_hermes.audit import HermesAuditStore, parse_audit_options
from grix_hermes.contract import (
    CMD_AUDIT_STATE,
    LOCAL_ACTION_AUDIT_GET_MANIFEST,
    STABLE_PUBLIC_COMMANDS,
)
from grix_hermes.protocol import GrixConnectionConfig, build_auth_payload


def test_parse_audit_options_matches_connector_shape():
    parsed = parse_audit_options({"extra": {"audit": {"enabled": True, "scope": "turn", "profile": "full"}}})
    assert parsed["state"] == "enabled"
    assert parsed["options"]["scope"] == "turn"
    assert parsed["options"]["capture"]["thinking"] is False
    assert parsed["options"]["capture"]["raw_provider_body"] is False
    snake = parse_audit_options({"audit": {"enabled": True, "capture": {"inputOutput": None, "input_output": False}}})
    assert snake["options"]["capture"]["input_output"] is False


def test_audit_contract_is_advertised():
    payload = build_auth_payload(
        GrixConnectionConfig(endpoint="ws://example.test", api_key="key", agent_id="1")
    )
    assert LOCAL_ACTION_AUDIT_GET_MANIFEST in payload["local_actions"]
    assert any(item["cmd"] == CMD_AUDIT_STATE for item in STABLE_PUBLIC_COMMANDS)


def test_store_finalizes_and_serves_replay_actions(tmp_path: Path):
    store = HermesAuditStore(str(tmp_path / "audit"))
    replay = store.finalize(
        audit_id="audit-1",
        turn_id="turn-1",
        session_id="session-1",
        event_id="event-1",
        msg_id="msg-1",
        provider="hermes",
        started_at=100,
        input_text="hello",
        output_text="world",
        outcome="responded",
    )
    assert replay["quality"]["status"] == "partial"
    manifest = store.action(
        "audit_get_manifest",
        {"session_id": "session-1", "audit_id": "audit-1", "turn_id": "turn-1"},
    )
    assert manifest["status"] == "ok"
    assert manifest["result"]["content_refs"]
    chunk = store.action(
        "audit_get_content_chunk",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "content_id": hashlib.sha256(b"world").hexdigest(),
        },
    )
    assert chunk["result"]["value"] == "world"
    assert (tmp_path / "audit").stat().st_mode & 0o777 == 0o700


def test_store_rejects_cross_session_lookup(tmp_path: Path):
    store = HermesAuditStore(str(tmp_path / "audit"))
    store.finalize(
        audit_id="audit-1",
        turn_id="turn-1",
        session_id="session-1",
        event_id="event-1",
        msg_id=None,
        provider="hermes",
        started_at=100,
        input_text="hello",
        output_text="world",
        outcome="failed",
    )
    result = store.action(
        "audit_get_manifest",
        {"session_id": "session-2", "audit_id": "audit-1", "turn_id": "turn-1"},
    )
    assert result["status"] == "failed"
    assert result["error_code"] == "AUDIT_NOT_FOUND"


def test_store_cursor_is_signed_and_utf8_safe(tmp_path: Path):
    store = HermesAuditStore(str(tmp_path / "audit"))
    store.finalize(
        audit_id="audit-1",
        turn_id="turn-1",
        session_id="session-1",
        event_id="event-1",
        msg_id=None,
        provider="hermes",
        started_at=100,
        input_text="你好",
        output_text="世界",
        outcome="responded",
    )
    spans = store.action(
        "audit_list_spans",
        {"session_id": "session-1", "audit_id": "audit-1", "turn_id": "turn-1", "limit": 1},
    )
    assert spans["status"] == "ok"
    assert spans["result"]["next_cursor"] is None

    content_id = hashlib.sha256("世界".encode()).hexdigest()
    first = store.action(
        "audit_get_content_chunk",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "content_id": content_id,
            "max_bytes": 4,
        },
    )
    assert first["result"]["value"] == "世"
    assert first["result"]["etag"] == content_id
    second = store.action(
        "audit_get_content_chunk",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "content_id": content_id,
            "max_bytes": 4,
            "cursor": first["result"]["next_cursor"],
        },
    )
    assert second["result"]["value"] == "界"
    tampered = first["result"]["next_cursor"][:-1] + ("A" if first["result"]["next_cursor"][-1] != "A" else "B")
    invalid = store.action(
        "audit_get_content_chunk",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "content_id": content_id,
            "cursor": tampered,
        },
    )
    assert invalid["error_code"] == "AUDIT_CURSOR_INVALID"
