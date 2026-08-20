"""Persist Grix relay credentials in the active standalone Hermes profile.

This module intentionally uses Hermes' own configuration API.  Apart from
keeping the write format aligned with ``/model``, that API performs the
atomic write and preserves host-specific config details that a YAML rewrite
would lose.
"""
from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse


GRIX_PROVIDER_ID = "grix"
_STATE_FILE = ".grix-relay-state.json"
_MISSING = object()
_CONFIG_LOCK = threading.Lock()


class RelayCredentialError(ValueError):
    """A safe, user-presentable relay configuration error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class RelayCredentials:
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class RelayConfigurationSnapshot:
    """In-memory pre-change config for a model-switch rollback.

    This snapshot is intentionally never serialized or returned to Grix: it can
    contain the user's prior provider credentials.  It only spans the local
    `/model` command issued by the same action.
    """

    config: Dict[str, Any]
    relay_state: Optional[bytes]


@dataclass(frozen=True)
class RelayLocalState:
    """The non-secret relay state needed by the server reconciliation protocol."""

    enabled: bool
    model: Optional[str]


def read_relay_local_state(hermes_home: str) -> RelayLocalState:
    """Read only whether this profile currently selects Grix and its model."""
    home = str(Path(hermes_home).expanduser())
    with _CONFIG_LOCK, _using_hermes_home(home):
        read_raw_config, _, _ = _host_config_api()
        config = _read_raw_config(read_raw_config, home)
    model = config.get("model")
    enabled = _model_uses_grix(config)
    selected = str(model.get("default") or "").strip() if isinstance(model, dict) else ""
    return RelayLocalState(enabled=enabled, model=selected or None)


def relay_credentials_from_params(params: Dict[str, Any]) -> Optional[RelayCredentials]:
    """Read current and transitional downlink field names without logging secrets."""
    base_url = _first_text(params, "openai_base_url", "base_url", "api_base_url")
    api_key = _first_text(params, "api_key", "virtual_key", "virtualKey")
    model = _first_text(params, "model", "model_id")

    if not base_url and not api_key:
        return None
    if not base_url:
        raise RelayCredentialError("missing_base_url", "openai_base_url is required to enable Grix relay")
    if not api_key:
        raise RelayCredentialError("missing_api_key", "api_key is required to enable Grix relay")
    if not model:
        raise RelayCredentialError("missing_model", "model is required to enable Grix relay")
    if any(char.isspace() for char in api_key):
        raise RelayCredentialError("invalid_api_key", "api_key must not contain whitespace")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RelayCredentialError("invalid_base_url", "openai_base_url must be an absolute HTTP(S) URL")
    return RelayCredentials(base_url=base_url, api_key=api_key, model=model)


def configure_relay_credentials(
    hermes_home: str,
    *,
    credentials: Optional[RelayCredentials] = None,
    disable: bool = False,
) -> Dict[str, Any]:
    """Enable or disable relay for one already-running standalone profile.

    A previous ``model`` section is kept in a mode-0600 sidecar so disable can
    restore the user's selection.  The Grix provider is deliberately retained
    on disable: the virtual key is long-lived for this agent and can be reused
    when relay is enabled again.
    """
    if disable and credentials is not None:
        raise RelayCredentialError("invalid_params", "disable cannot be combined with relay credentials")
    if not disable and credentials is None:
        raise RelayCredentialError("missing_api_key", "api_key is required to enable Grix relay")

    result, _ = _configure_relay_credentials(
        hermes_home,
        credentials=credentials,
        disable=disable,
        capture_snapshot=False,
    )
    return result


def configure_relay_credentials_for_model_switch(
    hermes_home: str,
    *,
    credentials: RelayCredentials,
) -> Tuple[Dict[str, Any], RelayConfigurationSnapshot]:
    """Enable relay and retain an in-memory rollback point for `/model`.

    Hermes needs the provider in its profile before it can execute the model
    command.  If that command fails, callers must restore this snapshot so a
    failed compatibility action cannot leave relay credentials configured.
    """
    result, snapshot = _configure_relay_credentials(
        hermes_home,
        credentials=credentials,
        disable=False,
        capture_snapshot=True,
    )
    assert snapshot is not None
    return result, snapshot


def configure_relay_credentials_with_snapshot(
    hermes_home: str,
    *,
    credentials: Optional[RelayCredentials] = None,
    disable: bool = False,
) -> Tuple[Dict[str, Any], RelayConfigurationSnapshot]:
    """Apply either relay transition with an in-memory rollback point."""
    result, snapshot = _configure_relay_credentials(
        hermes_home,
        credentials=credentials,
        disable=disable,
        capture_snapshot=True,
    )
    assert snapshot is not None
    return result, snapshot


def restore_relay_configuration(hermes_home: str, snapshot: RelayConfigurationSnapshot) -> None:
    """Restore a pre-change profile after the paired Hermes model command fails."""
    home = str(Path(hermes_home).expanduser())
    with _CONFIG_LOCK, _using_hermes_home(home):
        read_raw_config, save_config, _ = _host_config_api()
        save_config(copy.deepcopy(snapshot.config))
        _restore_state_file(home, snapshot.relay_state)
        _read_raw_config(read_raw_config, home)


def _configure_relay_credentials(
    hermes_home: str,
    *,
    credentials: Optional[RelayCredentials],
    disable: bool,
    capture_snapshot: bool,
) -> Tuple[Dict[str, Any], Optional[RelayConfigurationSnapshot]]:
    if disable and credentials is not None:
        raise RelayCredentialError("invalid_params", "disable cannot be combined with relay credentials")
    if not disable and credentials is None:
        raise RelayCredentialError("missing_api_key", "api_key is required to enable Grix relay")

    home = str(Path(hermes_home).expanduser())
    with _CONFIG_LOCK, _using_hermes_home(home):
        read_raw_config, save_config, clear_endpoint_credentials = _host_config_api()
        config = _read_raw_config(read_raw_config, home)
        rollback_snapshot = RelayConfigurationSnapshot(copy.deepcopy(config), _read_state_file(home))
        try:
            if disable:
                changed, restored = _disable_relay(config, home)
                if changed:
                    save_config(config)
                    _verify_config(read_raw_config, home, expect_relay=False)
                return (
                    {"relay": "disabled", "restored_model": restored, "restart_required": changed},
                    rollback_snapshot if capture_snapshot else None,
                )

            assert credentials is not None
            already_enabled = _model_uses_grix(config)
            if not already_enabled:
                _write_state(home, config.get("model", _MISSING))
            model_config = _model_section(config)
            providers = config.get("providers")
            if not isinstance(providers, dict):
                providers = {}
                config["providers"] = providers
            providers[GRIX_PROVIDER_ID] = {
                "name": "Grix",
                "api": credentials.base_url,
                "api_key": credentials.api_key,
                "default_model": credentials.model,
                "transport": "openai_chat",
            }
            clear_endpoint_credentials(model_config, clear_base_url=True)
            model_config["provider"] = GRIX_PROVIDER_ID
            model_config["default"] = credentials.model
            save_config(config)
            _verify_config(read_raw_config, home, expect_relay=True)
            return (
                {
                    "relay": "enabled",
                    "model": credentials.model,
                    "provider": GRIX_PROVIDER_ID,
                    "restart_required": True,
                },
                rollback_snapshot if capture_snapshot else None,
            )
        except Exception as exc:
            try:
                save_config(copy.deepcopy(rollback_snapshot.config))
                _restore_state_file(home, rollback_snapshot.relay_state)
            except Exception as rollback_exc:
                raise RelayCredentialError(
                    "config_rollback_failed",
                    "Hermes could not restore the previous profile after relay configuration failed",
                ) from rollback_exc
            raise exc


def redact_relay_secret(message: object, secret: str) -> str:
    """Ensure a host exception can never echo a downlinked key to logs/results."""
    text = str(message)
    return text.replace(secret, "***redacted***") if secret else text


def _first_text(params: Dict[str, Any], *names: str) -> str:
    for name in names:
        value = params.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _host_config_api() -> Tuple[Callable[[], Dict[str, Any]], Callable[[Dict[str, Any]], None], Callable[..., None]]:
    try:
        from hermes_cli.config import clear_model_endpoint_credentials, read_raw_config, save_config
    except Exception as exc:  # noqa: BLE001 - host packages are optional in unit tests
        raise RelayCredentialError("hermes_config_unavailable", "Hermes configuration API is unavailable") from exc
    return read_raw_config, save_config, clear_model_endpoint_credentials


@contextlib.contextmanager
def _using_hermes_home(home: str):
    previous = os.environ.get("HERMES_HOME")
    os.environ["HERMES_HOME"] = home
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous


def _read_raw_config(read_raw_config: Callable[[], Dict[str, Any]], home: str) -> Dict[str, Any]:
    config = read_raw_config() or {}
    if not isinstance(config, dict):
        raise RelayCredentialError("config_unreadable", "Hermes config.yaml has an invalid top-level value")
    if not config:
        path = Path(home) / "config.yaml"
        try:
            malformed = path.exists() and path.stat().st_size > 0
        except OSError:
            malformed = False
        if malformed:
            raise RelayCredentialError("config_unreadable", "Hermes config.yaml could not be parsed")
    return config


def _model_section(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("model")
    if isinstance(raw, dict):
        return raw
    section: Dict[str, Any] = {}
    if isinstance(raw, str) and raw.strip():
        section["default"] = raw.strip()
    config["model"] = section
    return section


def _model_uses_grix(config: Dict[str, Any]) -> bool:
    model = config.get("model")
    return isinstance(model, dict) and str(model.get("provider") or "").strip() == GRIX_PROVIDER_ID


def _state_path(home: str) -> Path:
    return Path(home) / _STATE_FILE


def _read_state_file(home: str) -> Optional[bytes]:
    try:
        return _state_path(home).read_bytes()
    except OSError:
        return None


def _restore_state_file(home: str, payload: Optional[bytes]) -> None:
    path = _state_path(home)
    if payload is None:
        with contextlib.suppress(OSError):
            path.unlink()
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(payload)


def _write_state(home: str, previous_model: object) -> None:
    payload = {"has_previous_model": previous_model is not _MISSING}
    if previous_model is not _MISSING:
        payload["previous_model"] = copy.deepcopy(previous_model)
    path = _state_path(home)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(payload, handle, ensure_ascii=False)


def _disable_relay(config: Dict[str, Any], home: str) -> Tuple[bool, bool]:
    if not _model_uses_grix(config):
        return False, False
    state = _read_state(home)
    if state and state.get("has_previous_model") is True:
        config["model"] = copy.deepcopy(state.get("previous_model"))
        restored = True
    else:
        model = _model_section(config)
        model.pop("provider", None)
        model.pop("default", None)
        restored = False
    with contextlib.suppress(OSError):
        _state_path(home).unlink()
    return True, restored


def _read_state(home: str) -> Optional[Dict[str, Any]]:
    try:
        with _state_path(home).open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _verify_config(read_raw_config: Callable[[], Dict[str, Any]], home: str, *, expect_relay: bool) -> None:
    fresh = _read_raw_config(read_raw_config, home)
    provider_present = isinstance(fresh.get("providers"), dict) and GRIX_PROVIDER_ID in fresh["providers"]
    active = _model_uses_grix(fresh)
    if expect_relay and not (provider_present and active):
        raise RelayCredentialError("config_write_failed", "Hermes did not persist the Grix relay settings")
    if not expect_relay and active:
        raise RelayCredentialError("config_write_failed", "Hermes did not disable the Grix relay settings")
