"""adapter 侧配额接线测试：config.yaml 凭据解析 + 工具栏 meta 增强。

走 stub 模式（同 test_queue_ops_adapter.py），不依赖 hermes-agent host。
"""

import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "tools" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        reg = types.ModuleType("tools.registry")

        class _Registry:
            def register(self, **kw):
                pass

        reg.registry = _Registry()
        reg.tool_error = lambda msg: f"ERR:{msg}"
        reg.tool_result = lambda obj: f"OK:{obj}"
        tools_pkg.registry = reg
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg

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
        gw_session.build_session_key = lambda *a, **kw: "k"

        gw_platforms = types.ModuleType("gateway.platforms")
        gw_platforms_base = types.ModuleType("gateway.platforms.base")
        gw_platforms_base.BasePlatformAdapter = object
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


_install_stubs()

import grix_hermes.adapter as adapter_mod  # noqa: E402


def _write_config(home: Path, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def test_resolve_source_from_model_section(tmp_path):
    _write_config(
        tmp_path,
        """
model:
  provider: deepseek-api
  base_url: https://api.deepseek.com
  api_key: sk-direct
  default: deepseek-v4-flash
""",
    )
    source = adapter_mod.resolve_provider_quota_source(str(tmp_path), {})
    assert source is not None
    assert source["baseUrl"] == "https://api.deepseek.com"
    assert source["apiKey"] == "sk-direct"
    assert source["providerId"] == "deepseek"


def test_resolve_source_fills_from_providers_key_env(tmp_path):
    _write_config(
        tmp_path,
        """
model:
  provider: deepseek-api
  base_url: http://127.0.0.1:8045/v1
  api_key: your-api-key-1
  default: deepseek-v4-flash
providers:
  deepseek-api:
    api: https://api.deepseek.com
    key_env: DEEPSEEK_API_KEY
""",
    )
    # 本地中转 + 占位 api_key：配额必须回落到 providers.api + key_env
    source = adapter_mod.resolve_provider_quota_source(
        str(tmp_path), {"DEEPSEEK_API_KEY": "sk-from-env"}
    )
    assert source is not None
    assert source["baseUrl"] == "https://api.deepseek.com"
    assert source["apiKey"] == "sk-from-env"
    assert source["providerId"] == "deepseek"


def test_resolve_source_keeps_direct_model_credentials(tmp_path):
    _write_config(
        tmp_path,
        """
model:
  provider: deepseek-api
  base_url: https://api.deepseek.com
  api_key: sk-direct
  default: deepseek-v4-flash
providers:
  deepseek-api:
    api: https://api.deepseek.com
    key_env: DEEPSEEK_API_KEY
""",
    )
    source = adapter_mod.resolve_provider_quota_source(
        str(tmp_path), {"DEEPSEEK_API_KEY": "sk-from-env"}
    )
    assert source is not None
    assert source["baseUrl"] == "https://api.deepseek.com"
    assert source["apiKey"] == "sk-direct"
    assert source["providerId"] == "deepseek"


def test_resolve_source_model_without_key_uses_key_env(tmp_path):
    _write_config(
        tmp_path,
        """
model:
  provider: kimi
  default: k3
providers:
  kimi:
    api: https://api.kimi.com/coding
    key_env: KIMI_API_KEY
""",
    )
    source = adapter_mod.resolve_provider_quota_source(
        str(tmp_path), {"KIMI_API_KEY": "sk-kimi-env"}
    )
    assert source is not None
    assert source["baseUrl"] == "https://api.kimi.com/coding"
    assert source["apiKey"] == "sk-kimi-env"
    assert source["providerId"] == "kimi"


def test_resolve_source_env_fallback_for_grix_relay(tmp_path):
    """connector spawn 注入的 grix 中转通道：config 无凭据时回落 GRIX_* env。"""
    _write_config(tmp_path, "model:\n  default: deepseek-v4-flash\n")
    source = adapter_mod.resolve_provider_quota_source(
        str(tmp_path),
        {
            "GRIX_HERMES_BASE_URL": "https://grix.dhf.pub/openai/v1",
            "GRIX_PROVIDER_API_KEY": "gwk_live_x",
        },
    )
    assert source is not None
    assert source["baseUrl"] == "https://grix.dhf.pub/openai/v1"
    assert source["apiKey"] == "gwk_live_x"
    assert source["providerId"] == "deepseek"  # 模型名嗅探


def test_resolve_source_missing_credentials_returns_none(tmp_path):
    _write_config(tmp_path, "model:\n  default: gpt-5.4\n")
    assert adapter_mod.resolve_provider_quota_source(str(tmp_path), {}) is None


def test_resolve_source_missing_config_returns_none(tmp_path):
    assert adapter_mod.resolve_provider_quota_source(str(tmp_path), {}) is None


# ── 工具栏 meta 增强 ──


def _bare_adapter():
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "test"
    return inst


def test_toolbar_meta_empty_without_quota():
    inst = _bare_adapter()
    inst._provider_quota = None
    inst._provider_quota_sampled_at_ms = 0
    assert inst._provider_quota_toolbar_meta() == {}


def test_toolbar_meta_enriches_provider_quota_and_rate_limits():
    inst = _bare_adapter()
    inst._provider_quota = {
        "provider": "kimi",
        "providerLabel": "Kimi",
        "planName": None,
        "tiers": [
            {"name": "five_hour", "label": "5h", "usedPercent": 12.5, "resetsAt": None},
            {"name": "weekly_limit", "label": "W", "usedPercent": 30.0, "resetsAt": None},
        ],
        "balance": None,
        "success": True,
        "error": None,
    }
    inst._provider_quota_sampled_at_ms = 1700000000000
    meta = inst._provider_quota_toolbar_meta()
    assert meta["provider_quota"]["provider"] == "kimi"
    assert meta["rate_limits"]["fiveHour"]["usedPercentage"] == 12.5
    assert meta["rate_limits"]["sevenDay"]["usedPercentage"] == 30.0
    assert meta["rate_limits"]["sampledAt"] == 1700000000000


def test_toolbar_meta_skips_failed_quota():
    inst = _bare_adapter()
    inst._provider_quota = {"provider": "kimi", "success": False, "error": "401"}
    inst._provider_quota_sampled_at_ms = 0
    assert inst._provider_quota_toolbar_meta() == {}
