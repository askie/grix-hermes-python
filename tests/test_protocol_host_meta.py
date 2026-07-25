from grix_hermes.protocol import (
    GrixConnectionConfig,
    build_auth_payload,
    resolve_event_queue_settings,
)


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
    assert "skill_upload" in payload["local_actions"]
    assert "skill_enable" in payload["local_actions"]
    assert "skill_disable" in payload["local_actions"]
    assert payload["host_meta"]["tailnet_ip"] == "100.64.0.5"
    assert payload["host_meta"]["file_server_port"] == 34567


def test_resolve_event_queue_settings_run_timeout_default_and_override():
    # 默认开启 30 分钟运行看门狗
    settings = resolve_event_queue_settings({})
    assert settings["run_timeout_ms"] == 1_800_000
    # 可通过 event_queue 配置段覆盖 / 关闭（0）
    assert resolve_event_queue_settings({"event_queue": {"run_timeout_ms": 0}})["run_timeout_ms"] == 0
    assert (
        resolve_event_queue_settings({"event_queue": {"run_timeout_ms": 60_000}})["run_timeout_ms"]
        == 60_000
    )


def test_build_auth_payload_concurrency_excludes_run_timeout():
    # 握手 concurrency 描述符只带 connector 对齐字段，本地看门狗字段不下发
    payload = build_auth_payload(
        GrixConnectionConfig(
            endpoint="ws://example.test",
            api_key="key",
            agent_id="123",
            concurrency=resolve_event_queue_settings({}),
        )
    )
    concurrency = payload["concurrency"]
    assert "run_timeout_ms" not in concurrency
    assert concurrency["max_queued"] == 5
    assert concurrency["cancelable_running"] is True
