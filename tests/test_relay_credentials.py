import copy
from unittest.mock import patch

import pytest

from grix_hermes.relay_credentials import (
    GRIX_PROVIDER_ID,
    RelayCredentialError,
    configure_relay_credentials,
    configure_relay_credentials_for_model_switch,
    read_relay_local_state,
    relay_credentials_from_params,
    restore_relay_configuration,
)


def _ops(config):
    saved = []

    def read_raw_config():
        return config

    def save_config(value):
        persisted = copy.deepcopy(value)
        saved.append(persisted)
        config.clear()
        config.update(persisted)

    def clear_model_endpoint_credentials(model, *, clear_base_url):
        assert clear_base_url is True
        model.pop("api_key", None)
        model.pop("base_url", None)

    return (read_raw_config, save_config, clear_model_endpoint_credentials), saved


def test_configure_relay_persists_provider_and_switches_model(tmp_path):
    config = {
        "providers": {"custom": {"api": "https://previous.example", "api_key": "old-secret"}},
        "model": {"provider": "custom", "default": "old-model", "api_key": "old-secret"},
    }
    ops, saved = _ops(config)
    credentials = relay_credentials_from_params({
        "openai_base_url": "https://relay.example/openai",
        "virtualKey": "new-secret",
        "model": "deepseek-v4-flash",
    })

    with patch("grix_hermes.relay_credentials._host_config_api", return_value=ops):
        result = configure_relay_credentials(str(tmp_path), credentials=credentials)

    assert result == {
        "relay": "enabled",
        "model": "deepseek-v4-flash",
        "provider": GRIX_PROVIDER_ID,
        "restart_required": True,
    }
    assert len(saved) == 1
    assert config["providers"][GRIX_PROVIDER_ID] == {
        "name": "Grix",
        "api": "https://relay.example/openai",
        "api_key": "new-secret",
        "default_model": "deepseek-v4-flash",
        "transport": "openai_chat",
    }
    assert config["model"] == {"provider": GRIX_PROVIDER_ID, "default": "deepseek-v4-flash"}
    state = (tmp_path / ".grix-relay-state.json").read_text(encoding="utf-8")
    assert "new-secret" not in state
    assert "old-secret" in state


def test_disable_relay_restores_previous_model_and_keeps_grix_provider(tmp_path):
    config = {"providers": {}, "model": {"provider": "custom", "default": "old-model"}}
    ops, _saved = _ops(config)
    credentials = relay_credentials_from_params({
        "openai_base_url": "https://relay.example/openai",
        "api_key": "new-secret",
        "model": "deepseek-v4-flash",
    })

    with patch("grix_hermes.relay_credentials._host_config_api", return_value=ops):
        configure_relay_credentials(str(tmp_path), credentials=credentials)
        result = configure_relay_credentials(str(tmp_path), disable=True)

    assert result == {"relay": "disabled", "restored_model": True, "restart_required": True}
    assert config["model"] == {"provider": "custom", "default": "old-model"}
    assert GRIX_PROVIDER_ID in config["providers"]
    assert not (tmp_path / ".grix-relay-state.json").exists()


def test_model_switch_rollback_restores_profile_and_sidecar(tmp_path):
    original = {
        "providers": {"custom": {"api": "https://previous.example", "api_key": "old-secret"}},
        "model": {"provider": "custom", "default": "old-model", "api_key": "old-secret"},
    }
    config = copy.deepcopy(original)
    ops, _saved = _ops(config)
    credentials = relay_credentials_from_params({
        "openai_base_url": "https://relay.example/openai",
        "api_key": "new-secret",
        "model": "deepseek-v4-flash",
    })

    with patch("grix_hermes.relay_credentials._host_config_api", return_value=ops):
        _result, snapshot = configure_relay_credentials_for_model_switch(
            str(tmp_path), credentials=credentials
        )
        restore_relay_configuration(str(tmp_path), snapshot)

    assert config == original
    assert not (tmp_path / ".grix-relay-state.json").exists()


def test_configure_relay_rolls_back_when_host_verification_fails(tmp_path):
    original = {"providers": {}, "model": {"provider": "custom", "default": "old-model"}}
    persisted = copy.deepcopy(original)
    saved = []

    def read_raw_config():
        return copy.deepcopy(persisted)

    def save_config(value):
        saved.append(copy.deepcopy(value))

    def clear_model_endpoint_credentials(model, *, clear_base_url):
        model.pop("api_key", None)
        model.pop("base_url", None)

    credentials = relay_credentials_from_params({
        "openai_base_url": "https://relay.example/openai",
        "api_key": "new-secret",
        "model": "deepseek-v4-flash",
    })
    with patch(
        "grix_hermes.relay_credentials._host_config_api",
        return_value=(read_raw_config, save_config, clear_model_endpoint_credentials),
    ), pytest.raises(RelayCredentialError, match="did not persist"):
        configure_relay_credentials(str(tmp_path), credentials=credentials)

    assert saved[-1] == original
    assert not (tmp_path / ".grix-relay-state.json").exists()


def test_relay_credentials_require_complete_safe_values():
    with pytest.raises(RelayCredentialError, match="openai_base_url") as missing_url:
        relay_credentials_from_params({"api_key": "secret", "model": "m"})
    assert missing_url.value.code == "missing_base_url"

    with pytest.raises(RelayCredentialError, match="absolute HTTP") as invalid_url:
        relay_credentials_from_params({"openai_base_url": "not-a-url", "api_key": "secret", "model": "m"})
    assert invalid_url.value.code == "invalid_base_url"


def test_read_relay_local_state_only_exposes_non_secret_selection(tmp_path):
    config = {
        "providers": {GRIX_PROVIDER_ID: {"api_key": "must-not-be-returned"}},
        "model": {"provider": GRIX_PROVIDER_ID, "default": "deepseek-v4-flash"},
    }
    ops, _saved = _ops(config)

    with patch("grix_hermes.relay_credentials._host_config_api", return_value=ops):
        state = read_relay_local_state(str(tmp_path))

    assert state.enabled is True
    assert state.model == "deepseek-v4-flash"
    assert "must-not-be-returned" not in repr(state)
