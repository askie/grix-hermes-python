"""Unit tests for profile-namespaced session_key generation and route resolution.

Ensures non-default profiles (e.g. appstore, shujiyuan) in multiplexed Hermes
gateways derive matching session keys between BasePlatformAdapter and GrixAdapter,
preventing premature completion (status=responded) and enabling proper route resolution.
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _fake_build_session_key(
    source,
    group_sessions_per_user=True,
    thread_sessions_per_user=False,
    profile=None,
):
    ns = f"agent:{profile}" if profile and profile != "default" else "agent:main"
    p = getattr(getattr(source, "platform", None), "value", "grix")
    ct = getattr(source, "chat_type", "dm")
    cid = getattr(source, "chat_id", "")
    return f"{ns}:{p}:{ct}:{cid}"


def _ensure_stubs():
    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw_cfg = types.ModuleType("gateway.config")

        class _Platform:
            def __init__(self, name):
                self.value = name

            def __eq__(self, other):
                return getattr(other, "value", None) == self.value

            def __hash__(self):
                return hash(self.value)

        gw_cfg.Platform = _Platform
        gw_cfg.PlatformConfig = lambda **kw: SimpleNamespace(**kw)

        gw_session = types.ModuleType("gateway.session")
        gw_session.build_session_key = _fake_build_session_key

        gw_platforms = types.ModuleType("gateway.platforms")
        gw_platforms_base = types.ModuleType("gateway.platforms.base")

        class _BasePlatformAdapter:
            def __init__(self, config=None, platform=None):
                self.config = config or SimpleNamespace(extra={})
                self.platform = platform

            def _canonicalize(self, source):
                pass

            def _session_key_profile(self, source=None):
                return getattr(source, "profile", None) or getattr(self, "_owner_profile", None)

            def _source_session_key(self, source):
                self._canonicalize(source)
                extra = getattr(self.config, "extra", {}) or {}
                return _fake_build_session_key(
                    source,
                    group_sessions_per_user=extra.get("group_sessions_per_user", True),
                    thread_sessions_per_user=extra.get("thread_sessions_per_user", False),
                    profile=self._session_key_profile(source),
                )

        gw_platforms_base.BasePlatformAdapter = _BasePlatformAdapter
        gw_platforms_base.MessageEvent = type("MessageEvent", (), {})
        gw_platforms_base.MessageType = type("MessageType", (), {"TEXT": "text"})
        gw_platforms_base.ProcessingOutcome = type(
            "ProcessingOutcome", (), {"SUCCESS": object(), "CANCELLED": object()}
        )
        gw_platforms_base.SendResult = type("SendResult", (), {})

        gw_run = types.ModuleType("gateway.run")
        gw_run._gateway_runner_ref = lambda: None

        sys.modules["gateway"] = gw
        sys.modules["gateway.config"] = gw_cfg
        sys.modules["gateway.session"] = gw_session
        sys.modules["gateway.platforms"] = gw_platforms
        sys.modules["gateway.platforms.base"] = gw_platforms_base
        sys.modules["gateway.run"] = gw_run
    else:
        gw_session = sys.modules.get("gateway.session")
        if gw_session:
            gw_session.build_session_key = _fake_build_session_key


_ensure_stubs()

from grix_hermes.adapter import (
    GrixAdapter,
    _is_route_session_key,
    _parse_route_session_key,
    resolve_grix_target,
)
from grix_hermes.protocol import GrixConnectionConfig


class DummySource:
    def __init__(self, chat_id="455e9cbf-5c60", chat_type="dm", profile=None):
        self.chat_id = chat_id
        self.chat_type = chat_type
        self.profile = profile
        self.platform = SimpleNamespace(value="grix")
        self.user_id = "user1"
        self.user_id_alt = None
        self.thread_id = None
        self.prospective_thread_id = None
        self.chat_name = "test"
        self.chat_topic = None


def test_is_and_parse_route_session_key_supports_named_profiles():
    # main profile
    main_key = "agent:main:grix:dm:455e9cbf-5c60-48f0-bf32-a5734e33a39e"
    assert _is_route_session_key(main_key) is True
    parsed_main = _parse_route_session_key(main_key)
    assert parsed_main == {
        "chat_type": "dm",
        "session_id": "455e9cbf-5c60-48f0-bf32-a5734e33a39e",
        "thread_id": None,
    }

    # appstore profile
    appstore_key = "agent:appstore:grix:dm:455e9cbf-5c60-48f0-bf32-a5734e33a39e"
    assert _is_route_session_key(appstore_key) is True
    parsed_appstore = _parse_route_session_key(appstore_key)
    assert parsed_appstore == {
        "chat_type": "dm",
        "session_id": "455e9cbf-5c60-48f0-bf32-a5734e33a39e",
        "thread_id": None,
    }

    # invalid key
    assert _is_route_session_key("agent:appstore:telegram:dm:123") is False
    assert _parse_route_session_key("agent:appstore:telegram:dm:123") is None


@pytest.mark.asyncio
async def test_resolve_grix_target_with_named_profile():
    connection = GrixConnectionConfig(
        endpoint="ws://dummy",
        agent_id="2062548732490027008",
        api_key="key",
    )
    target = "agent:appstore:grix:dm:455e9cbf-5c60-48f0-bf32-a5734e33a39e"
    resolved_id, thread_id = await resolve_grix_target(
        client=None,
        connection=connection,
        target=target,
    )
    assert resolved_id == "455e9cbf-5c60-48f0-bf32-a5734e33a39e"
    assert thread_id is None


def test_grix_adapter_source_session_key_inherits_profile(monkeypatch):
    import grix_hermes.adapter as adapter_mod
    monkeypatch.setattr(adapter_mod, "build_session_key", _fake_build_session_key)

    config = SimpleNamespace(extra={"agent_id": "2062548732490027008", "api_key": "k"})
    adapter = GrixAdapter.__new__(GrixAdapter)
    adapter.config = config
    adapter._owner_profile = "appstore"

    source = DummySource(chat_id="session-123", chat_type="dm")
    key = adapter._source_session_key(source)
    assert key == "agent:appstore:grix:dm:session-123"

    # Default profile fallback
    adapter._owner_profile = None
    key_default = adapter._source_session_key(source)
    assert key_default == "agent:main:grix:dm:session-123"
