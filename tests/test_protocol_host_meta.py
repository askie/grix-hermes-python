from grix_hermes.protocol import GrixConnectionConfig, build_auth_payload


def test_build_auth_payload_includes_tailnet_file_server_meta(monkeypatch):
    from grix_hermes import tailnet_file_server

    monkeypatch.setattr(
        tailnet_file_server,
        "host_meta_fields",
        lambda: {"tailnet_ip": "100.64.0.5", "file_server_port": 34567},
    )

    payload = build_auth_payload(
        GrixConnectionConfig(
            endpoint="ws://example.test",
            api_key="key",
            agent_id="123",
        )
    )

    assert "create_folder" in payload["local_actions"]
    assert payload["host_meta"]["tailnet_ip"] == "100.64.0.5"
    assert payload["host_meta"]["file_server_port"] == 34567
