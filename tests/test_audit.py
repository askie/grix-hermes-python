from pathlib import Path
import hashlib

from grix_hermes.audit import HermesAuditStore, parse_audit_options
from grix_hermes.contract import (
    CAP_AUDIT_REPLAY_V2,
    CMD_AUDIT_STATE,
    LOCAL_ACTION_AUDIT_GET_MANIFEST,
    STABLE_PUBLIC_COMMANDS,
)
from grix_hermes.protocol import GrixConnectionConfig, build_auth_payload, build_connection_config


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
    assert CAP_AUDIT_REPLAY_V2 in payload["capabilities"]
    assert any(item["cmd"] == CMD_AUDIT_STATE for item in STABLE_PUBLIC_COMMANDS)

    custom = build_auth_payload(
        GrixConnectionConfig(
            endpoint="ws://example.test",
            api_key="key",
            agent_id="1",
            capabilities=["local_action_v1"],
        )
    )
    assert CAP_AUDIT_REPLAY_V2 in custom["capabilities"]
    cfg = build_connection_config(
        {"endpoint": "ws://example.test", "agent_id": "1", "capabilities": ["local_action_v1"]},
        api_key="k",
    )
    assert CAP_AUDIT_REPLAY_V2 in cfg.capabilities


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
    assert replay["statistics"]["llm_request_count"] == 1
    assert replay["statistics"]["total_usage"]["input"]["cacheRead"] is None
    assert "cache_read" not in replay["statistics"]["total_usage"]["input"]
    kinds = [span["kind"] for span in replay["spans"]]
    assert kinds == ["turn", "llm_request"]
    manifest = store.action(
        "audit_get_manifest",
        {"session_id": "session-1", "audit_id": "audit-1", "turn_id": "turn-1"},
    )
    assert manifest["status"] == "ok"
    assert manifest["result"]["content_refs"]
    assert manifest["result"]["statistics"]["llm_request_count"] == 1
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
    assert spans["result"]["has_more"] is True
    assert spans["result"]["next_cursor"] is not None
    assert [item["kind"] for item in spans["result"]["items"]] == ["turn"]
    page2 = store.action(
        "audit_list_spans",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "limit": 1,
            "cursor": spans["result"]["next_cursor"],
        },
    )
    assert page2["status"] == "ok"
    assert [item["kind"] for item in page2["result"]["items"]] == ["llm_request"]
    assert page2["result"]["next_cursor"] is None

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


def test_store_coerces_intish_params(tmp_path: Path):
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
        outcome="responded",
    )
    spans = store.action(
        "audit_list_spans",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "revision": 1.0,
            "limit": 2.0,
        },
    )
    assert spans["status"] == "ok"
    assert len(spans["result"]["items"]) == 2
    chunk = store.action(
        "audit_get_content_chunk",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "content_id": hashlib.sha256(b"hello").hexdigest(),
            "max_bytes": 64.0,
        },
    )
    assert chunk["status"] == "ok"
    assert chunk["result"]["value"] == "hello"
    rejected = store.action(
        "audit_list_spans",
        {
            "session_id": "session-1",
            "audit_id": "audit-1",
            "turn_id": "turn-1",
            "limit": "2",
        },
    )
    assert rejected["status"] == "failed"
    assert rejected["error_code"] == "AUDIT_INVALID_PARAMS"


def test_store_upgrades_legacy_snake_case_replay(tmp_path: Path):
    store = HermesAuditStore(str(tmp_path / "audit"))
    legacy = {
        "audit_id": "audit-legacy",
        "turn_id": "turn-legacy",
        "revision": 1,
        "session_id": "session-1",
        "provider": "hermes",
        "outcome": "completed",
        "started_at": "2026-01-01T00:00:00.000Z",
        "finalized_at": "2026-01-01T00:00:01.000Z",
        "quality": {"status": "partial"},
        "statistics": {
            "span_count": 1,
            "span_counts": [{"kind": "turn", "name": "Hermes audited turn", "count": 1}],
            "llm_request_count": 0,
            "llm_requests": [],
            "total_usage": {
                "input": {
                    "total": 0,
                    "uncached": None,
                    "cache_read": None,
                    "cache_write": None,
                    "other": None,
                },
                "output": {"total": 0},
                "total_processed": 0,
            },
        },
        "spans": [{
            "span_id": "turn-legacy:turn",
            "trace_id": "audit-legacy",
            "turn_id": "turn-legacy",
            "session_id": "session-1",
            "kind": "turn",
            "name": "Hermes audited turn",
            "sequence": 0,
            "status": "completed",
            "started_at": "2026-01-01T00:00:00.000Z",
            "ended_at": "2026-01-01T00:00:01.000Z",
            "duration_ms": 1000,
            "input_refs": [],
            "output_refs": [],
        }],
        "content_refs": {},
        "manifest": {
            "audit_id": "audit-legacy",
            "turn_id": "turn-legacy",
            "revision": 1,
            "session_id": "session-1",
            "provider": "hermes",
            "status": "completed",
            "statistics": {
                "span_count": 1,
                "llm_request_count": 0,
                "total_usage": {
                    "input": {"total": 0, "cache_read": None, "cache_write": None},
                },
            },
            "has_spans": True,
            "content_refs": [],
        },
    }
    store.save(legacy)
    manifest = store.action(
        "audit_get_manifest",
        {"session_id": "session-1", "audit_id": "audit-legacy", "turn_id": "turn-legacy"},
    )
    assert manifest["status"] == "ok"
    usage_input = manifest["result"]["statistics"]["total_usage"]["input"]
    assert "cacheRead" in usage_input
    assert "cache_read" not in usage_input
    assert manifest["result"]["statistics"]["llm_request_count"] == 1
    spans = store.action(
        "audit_list_spans",
        {"session_id": "session-1", "audit_id": "audit-legacy", "turn_id": "turn-legacy"},
    )
    assert [item["kind"] for item in spans["result"]["items"]] == ["turn", "llm_request"]
