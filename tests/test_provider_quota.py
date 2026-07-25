"""provider_quota 平移的回归测试（对齐 connector tests/provider-quota.test.ts 等）。

HTTP 层统一走 provider_quota._http_get_json，测试用 monkeypatch 替换，
保证用例确定性、不触网。
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grix_hermes import provider_quota as pq
from grix_hermes.provider_quota_service import (
    ProviderQuotaService,
    resolve_quota_base_url,
)


def run(coro):
    return asyncio.run(coro)


# ── 探测函数（对齐 connector detectProvider / normalizeProviderId 用例） ──


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://open.bigmodel.cn/api/paas/v4", "zhipu"),
        ("https://api.z.ai/v4", "zhipu"),
        ("https://api.kimi.com/coding", "kimi"),
        ("https://api.minimaxi.com/v1", "minimax_cn"),
        ("https://api.minimax.io/v1", "minimax_en"),
        ("https://api.deepseek.com", "deepseek"),
        ("https://api.stepfun.ai/v1", "stepfun"),
        ("https://api.stepfun.com/v1", "stepfun"),
        ("https://api.siliconflow.cn/v1", "siliconflow_cn"),
        ("https://api.siliconflow.com/v1", "siliconflow_en"),
        ("https://openrouter.ai/api/v1", "openrouter"),
        ("https://api.novita.ai/v3", "novita"),
    ],
)
def test_detect_provider(url, expected):
    assert pq.detect_provider(url)[0] == expected


def test_detect_provider_unknown_and_case_insensitive():
    assert pq.detect_provider("https://api.unknown.com") is None
    assert pq.detect_provider("HTTPS://API.DEEPSEEK.COM")[0] == "deepseek"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("zhipu", "zhipu"),
        ("zai", "zhipu"),
        ("Z.AI", "zhipu"),
        ("glm", "zhipu"),
        ("bigmodel", "zhipu"),
        ("moonshot", "kimi"),
        ("minimax", "minimax_cn"),
        ("siliconflow", "siliconflow_cn"),
        ("deepseek", "deepseek"),
        ("unknown", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_provider_id(value, expected):
    assert pq.normalize_provider_id(value) == expected


@pytest.mark.parametrize(
    "model,expected",
    [
        ("glm-4.6", "zhipu"),
        ("zai/glm-4", "zhipu"),
        ("kimi-k3", "kimi"),
        ("moonshot-v1", "kimi"),
        ("deepseek-chat", "deepseek"),
        ("gpt-5.4", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_provider_from_model(model, expected):
    info = pq.detect_provider_from_model(model)
    assert (info[0] if info else None) == expected


# ── 各厂商查询（mock _http_get_json） ──


def _patch_http(monkeypatch, routes):
    """routes: url-substring → (status, body)"""

    async def fake_get(url, headers):
        for needle, response in routes.items():
            if needle in url:
                return response
        return (404, None)

    monkeypatch.setattr(pq, "_http_get_json", fake_get)
    return fake_get


def test_query_kimi_parses_5h_and_weekly(monkeypatch):
    _patch_http(
        monkeypatch,
        {
            "api.kimi.com": (
                200,
                {
                    "limits": [
                        {"detail": {"limit": 100, "remaining": 40, "resetTime": 1750000000}}
                    ],
                    "usage": {"limit": 1000, "remaining": 250, "resetTime": 1750600000},
                },
            )
        },
    )
    result = run(pq.query_provider_quota("https://api.kimi.com/coding", "sk-test"))
    assert result["success"] is True
    assert result["provider"] == "kimi"
    tiers = {t["name"]: t for t in result["tiers"]}
    assert tiers["five_hour"]["usedPercent"] == 60.0
    assert tiers["five_hour"]["resetsAt"] is not None
    assert tiers["weekly_limit"]["usedPercent"] == 75.0


def test_query_kimi_auth_failure(monkeypatch):
    _patch_http(monkeypatch, {"api.kimi.com": (401, {})})
    result = run(pq.query_provider_quota("https://api.kimi.com/coding", "bad"))
    assert result["success"] is False
    assert "401" in result["error"]


def test_query_zhipu_tiers_sorted_by_reset(monkeypatch):
    _patch_http(
        monkeypatch,
        {
            "api.z.ai": (
                200,
                {
                    "success": True,
                    "data": {
                        "level": "pro",
                        "limits": [
                            {"type": "TOKENS_LIMIT", "percentage": 80, "nextResetTime": 1750600000000},
                            {"type": "TOKENS_LIMIT", "percentage": 20, "nextResetTime": 1750000000000},
                            {"type": "RATE_LIMIT", "percentage": 1, "nextResetTime": 1750000000000},
                        ],
                    },
                },
            )
        },
    )
    result = run(pq.query_provider_quota("https://api.z.ai/v4", "key"))
    assert result["success"] is True
    assert result["planName"] == "pro"
    tiers = {t["name"]: t for t in result["tiers"]}
    # nearest reset = five_hour
    assert tiers["five_hour"]["usedPercent"] == 20
    assert tiers["weekly_limit"]["usedPercent"] == 80


def test_query_deepseek_balance(monkeypatch):
    _patch_http(
        monkeypatch,
        {
            "api.deepseek.com": (
                200,
                {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "12.34"}]},
            )
        },
    )
    result = run(pq.query_provider_quota("https://api.deepseek.com", "key"))
    assert result["success"] is True
    assert result["balance"] == {"remaining": 12.34, "total": None, "used": None, "unit": "CNY"}


def test_query_minimax_weekly(monkeypatch):
    _patch_http(
        monkeypatch,
        {
            "api.minimaxi.com": (
                200,
                {
                    "base_resp": {"status_code": 0},
                    "model_remains": [
                        {
                            "current_interval_total_count": 100,
                            "current_interval_usage_count": 30,
                            "end_time": 1750000000000,
                            "current_weekly_total_count": 500,
                            "current_weekly_usage_count": 100,
                            "weekly_end_time": 1750600000000,
                        }
                    ],
                },
            )
        },
    )
    result = run(pq.query_provider_quota("https://api.minimaxi.com/v1", "key"))
    tiers = {t["name"]: t for t in result["tiers"]}
    assert tiers["five_hour"]["usedPercent"] == 30.0
    assert tiers["weekly_limit"]["usedPercent"] == 20.0


def test_query_openrouter_credits(monkeypatch):
    _patch_http(
        monkeypatch,
        {"openrouter.ai": (200, {"data": {"total_credits": 10.0, "total_usage": 2.5}})},
    )
    result = run(pq.query_provider_quota("https://openrouter.ai/api/v1", "key"))
    assert result["balance"]["remaining"] == 7.5
    assert result["balance"]["unit"] == "USD"


def test_query_novita_scales_balance(monkeypatch):
    _patch_http(monkeypatch, {"api.novita.ai": (200, {"availableBalance": 123400})})
    result = run(pq.query_provider_quota("https://api.novita.ai/v3", "key"))
    assert result["balance"]["remaining"] == pytest.approx(12.34)


def test_query_stepfun_balance(monkeypatch):
    _patch_http(monkeypatch, {"api.stepfun.com": (200, {"balance": 88.5})})
    result = run(pq.query_provider_quota("https://api.stepfun.com/v1", "key"))
    assert result["balance"] == {"remaining": 88.5, "total": None, "used": None, "unit": "CNY"}


def test_query_siliconflow_balance(monkeypatch):
    _patch_http(monkeypatch, {"api.siliconflow.cn": (200, {"data": {"totalBalance": 66.0}})})
    result = run(pq.query_provider_quota("https://api.siliconflow.cn/v1", "key"))
    assert result["balance"]["remaining"] == 66.0


def test_query_empty_api_key():
    result = run(pq.query_provider_quota("https://api.kimi.com/coding", "  "))
    assert result["success"] is False
    assert "API key is empty" in result["error"]


def test_query_hint_routes_through_base_url(monkeypatch):
    """Opaque relay URL + explicit provider hint → query via base_url。"""
    calls = []

    async def fake_get(url, headers):
        calls.append(url)
        if "/coding/v1/usages" in url:
            return (200, {"usage": {"limit": 100, "remaining": 90, "resetTime": None}})
        return (404, None)

    monkeypatch.setattr(pq, "_http_get_json", fake_get)
    result = run(
        pq.query_provider_quota("https://relay.example.com/openai/v1", "key", "kimi")
    )
    assert result["success"] is True
    assert result["provider"] == "kimi"
    assert calls == ["https://relay.example.com/openai/v1/coding/v1/usages"]
    tiers = {t["name"]: t for t in result["tiers"]}
    assert tiers["weekly_limit"]["usedPercent"] == 10.0


def test_query_hint_unavailable_returns_error(monkeypatch):
    _patch_http(monkeypatch, {})  # 全部 404
    result = run(
        pq.query_provider_quota("https://relay.example.com/v1", "key", "deepseek")
    )
    assert result["success"] is False
    assert "unavailable through base URL" in result["error"]


def test_probe_identifies_provider_through_relay(monkeypatch):
    """无 hint 无 URL 特征 → 并发探测全部厂商，第一个成功者为真。"""
    pq._provider_probe_cache.clear()

    async def fake_get(url, headers):
        if "/user/balance" in url:
            return (
                200,
                {"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "5.0"}]},
            )
        return (404, None)

    monkeypatch.setattr(pq, "_http_get_json", fake_get)
    result = run(pq.query_provider_quota("https://relay.example.com/v1", "key"))
    assert result["success"] is True
    assert result["provider"] == "deepseek"
    # 探测缓存生效：第二次走缓存的 provider 直接命中
    result2 = run(pq.query_provider_quota("https://relay.example.com/v1", "key"))
    assert result2["provider"] == "deepseek"


def test_probe_unknown_provider(monkeypatch):
    pq._provider_probe_cache.clear()
    _patch_http(monkeypatch, {})
    result = run(pq.query_provider_quota("https://relay.example.com/v1", "key"))
    assert result["success"] is False
    assert "Could not identify provider" in result["error"]


def test_network_error_normalized(monkeypatch):
    async def boom(url, headers):
        raise OSError("connection refused")

    monkeypatch.setattr(pq, "_http_get_json", boom)
    result = run(pq.query_provider_quota("https://api.kimi.com/coding", "key"))
    assert result["success"] is False
    assert "Network error" in result["error"]


# ── 展示格式（对齐 providerQuotaToRateLimits） ──


def test_rate_limits_from_tiers():
    quota = {
        "provider": "kimi",
        "providerLabel": "Kimi",
        "planName": None,
        "tiers": [
            {"name": "five_hour", "label": "5h", "usedPercent": 60.0, "resetsAt": "2026-07-25T10:00:00.000Z"},
            {"name": "weekly_limit", "label": "W", "usedPercent": 75.0, "resetsAt": None},
        ],
        "balance": None,
        "success": True,
        "error": None,
    }
    rl = pq.provider_quota_to_rate_limits(quota, 123456789)
    assert rl["fiveHour"]["usedPercentage"] == 60.0
    assert rl["fiveHour"]["resetsAt"] > 0
    assert rl["sevenDay"] == {"usedPercentage": 75.0, "resetsAt": 0}
    assert rl["sampledAt"] == 123456789


def test_rate_limits_from_balance():
    quota = {
        "provider": "deepseek",
        "providerLabel": "DeepSeek",
        "planName": None,
        "tiers": [],
        "balance": {"remaining": 8.0, "total": 10.0, "used": 2.0, "unit": "CNY"},
        "success": True,
        "error": None,
    }
    rl = pq.provider_quota_to_rate_limits(quota, 1)
    assert rl["credit"] == {
        "remaining": 8.0,
        "total": 10.0,
        "used": 2.0,
        "unit": "CNY",
        "resetsAt": 0,
    }


def test_rate_limits_empty_returns_none():
    quota = {"tiers": [], "balance": None, "success": True}
    assert pq.provider_quota_to_rate_limits(quota) is None


# ── Service 缓存 ──


def _source(**overrides):
    base = {"baseUrl": "https://api.kimi.com/coding", "apiKey": "sk-test"}
    base.update(overrides)
    return base


def _ok_quota():
    return {
        "provider": "kimi",
        "providerLabel": "Kimi",
        "planName": None,
        "tiers": [{"name": "five_hour", "label": "5h", "usedPercent": 1.0, "resetsAt": None}],
        "balance": None,
        "success": True,
        "error": None,
    }


def test_service_caches_success(monkeypatch):
    calls = []

    async def fake_query(base_url, api_key, hint=None):
        calls.append(base_url)
        return _ok_quota()

    monkeypatch.setattr(
        "grix_hermes.provider_quota_service.query_provider_quota", fake_query
    )
    service = ProviderQuotaService(ttl_ms=60_000)
    first = run(service.query(_source()))
    second = run(service.query(_source()))
    assert first["cached"] is False
    assert second["cached"] is True
    assert len(calls) == 1


def test_service_fresh_bypasses_cache(monkeypatch):
    calls = []

    async def fake_query(base_url, api_key, hint=None):
        calls.append(1)
        return _ok_quota()

    monkeypatch.setattr(
        "grix_hermes.provider_quota_service.query_provider_quota", fake_query
    )
    service = ProviderQuotaService()
    run(service.query(_source()))
    run(service.query(_source(), fresh=True))
    assert len(calls) == 2


def test_service_does_not_cache_failure(monkeypatch):
    async def fake_query(base_url, api_key, hint=None):
        return {**_ok_quota(), "success": False, "error": "boom", "tiers": []}

    monkeypatch.setattr(
        "grix_hermes.provider_quota_service.query_provider_quota", fake_query
    )
    service = ProviderQuotaService()
    first = run(service.query(_source()))
    second = run(service.query(_source()))
    assert first["cached"] is False
    assert second["cached"] is False


def test_service_cache_key_isolates_credentials():
    service = ProviderQuotaService()
    key_a = service.cache_key(_source(apiKey="key-a"))
    key_b = service.cache_key(_source(apiKey="key-b"))
    assert key_a != key_b
    assert "key-a" not in key_a  # 只存指纹，不留原始凭据


def test_resolve_quota_base_url():
    assert resolve_quota_base_url({"baseUrl": "https://api.kimi.com/coding"}) == (
        "https://api.kimi.com/coding"
    )
    # opaque relay → 配额 API 根默认取 origin
    assert resolve_quota_base_url({"baseUrl": "https://relay.example.com/openai/v1"}) == (
        "https://relay.example.com"
    )
    assert resolve_quota_base_url(
        {"baseUrl": "https://relay.example.com/v1", "quotaBaseUrl": "https://quota.example.com/"}
    ) == "https://quota.example.com"
