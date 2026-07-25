"""大模型提供商用量限额查询（平移自 grix-connector src/core/provider-quota/）。

按当前生效的 provider（base_url + api_key）查询其配额 API，归一化为
ProviderQuotaResult（tiers: 5h/周限额窗口；balance: 余额），供工具栏展示。

与 connector 的差异（CLI 专属部分不平移）：
- 不含 kimi-code CLI 的 ~/.kimi-code OAuth 凭据解析（kimi 适配器专属）；
- 不含 kiro OAuth、pi models.json 凭据解析（各自 CLI 专属）；
  hermes 侧的凭据来源是 config.yaml（adapter.resolve_provider_quota_source）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, TypedDict

import aiohttp

TIMEOUT_S = 10.0

# ── 类型（对齐 connector types.ts，全部 plain dict 便于直接进 WS meta） ──


class QuotaTier(TypedDict, total=False):
    name: str  # "five_hour" | "weekly_limit" | "balance" ...
    label: str
    usedPercent: float
    resetsAt: Optional[str]  # ISO 8601


class BalanceInfo(TypedDict, total=False):
    remaining: float
    total: Optional[float]
    used: Optional[float]
    unit: str
    resetsAt: Optional[str]


class ProviderQuotaResult(TypedDict, total=False):
    provider: str
    providerLabel: str
    planName: Optional[str]
    tiers: List[QuotaTier]
    balance: Optional[BalanceInfo]
    success: bool
    error: Optional[str]


class ProviderQuotaSource(TypedDict, total=False):
    providerId: Optional[str]
    baseUrl: str
    quotaBaseUrl: Optional[str]
    apiKey: str
    accountKey: Optional[str]


# ── Provider 探测（对齐 providers.ts） ──

ProviderId = str  # 'zhipu' | 'kimi' | 'minimax_cn' | ... | 'novita'

_PROVIDER_IDS = frozenset(
    {
        "zhipu",
        "kimi",
        "minimax_cn",
        "minimax_en",
        "deepseek",
        "stepfun",
        "siliconflow_cn",
        "siliconflow_en",
        "openrouter",
        "novita",
    }
)

_PROVIDER_ALIASES = {
    "zai": "zhipu",
    "z.ai": "zhipu",
    "glm": "zhipu",
    "bigmodel": "zhipu",
    "moonshot": "kimi",
    "minimax": "minimax_cn",
    "siliconflow": "siliconflow_cn",
}


def normalize_provider_id(value: Optional[str]) -> Optional[ProviderId]:
    normalized = (value or "").strip().lower()
    if not normalized:
        return None
    aliased = _PROVIDER_ALIASES.get(normalized, normalized)
    return aliased if aliased in _PROVIDER_IDS else None


def detect_provider(base_url: str) -> Optional[Tuple[ProviderId, str]]:
    url = (base_url or "").lower()
    if "open.bigmodel.cn" in url or "bigmodel.cn" in url or "api.z.ai" in url:
        return ("zhipu", "Zhipu GLM")
    if "api.kimi.com" in url:
        return ("kimi", "Kimi")
    if "api.minimaxi.com" in url:
        return ("minimax_cn", "MiniMax")
    if "api.minimax.io" in url:
        return ("minimax_en", "MiniMax")
    if "api.deepseek.com" in url:
        return ("deepseek", "DeepSeek")
    if "api.stepfun.ai" in url or "api.stepfun.com" in url:
        return ("stepfun", "StepFun")
    if "api.siliconflow.cn" in url:
        return ("siliconflow_cn", "SiliconFlow")
    if "api.siliconflow.com" in url:
        return ("siliconflow_en", "SiliconFlow")
    if "openrouter.ai" in url:
        return ("openrouter", "OpenRouter")
    if "api.novita.ai" in url:
        return ("novita", "Novita AI")
    return None


_MODEL_SNIFF_RULES = (
    (re.compile(r"(^|[\/:_-])(glm|zhipu|zai)([\/:_.-]|$)"), ("zhipu", "Zhipu GLM")),
    (re.compile(r"(^|[\/:_-])(kimi|moonshot)([\/:_.-]|$)"), ("kimi", "Kimi")),
    (re.compile(r"(^|[\/:_-])deepseek([\/:_.-]|$)"), ("deepseek", "DeepSeek")),
)


def detect_provider_from_model(model: Optional[str]) -> Optional[Tuple[ProviderId, str]]:
    """Best-effort hint for opaque relay URLs. Explicit providerId still wins."""
    value = (model or "").strip().lower()
    if not value:
        return None
    for pattern, info in _MODEL_SNIFF_RULES:
        if pattern.search(value):
            return info
    return None


# ── Helpers ──


def _make_error(provider: str, label: str, msg: str) -> ProviderQuotaResult:
    return {
        "provider": provider,
        "providerLabel": label,
        "planName": None,
        "tiers": [],
        "balance": None,
        "success": False,
        "error": msg,
    }


def _parse_f64(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _ms_to_iso(ms: Optional[float]) -> Optional[str]:
    if ms is None or ms <= 0:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ms / 1000))
    except (OverflowError, OSError, ValueError):
        return None


def _extract_reset_time(value: Any) -> Optional[str]:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Auto-detect seconds vs milliseconds
        ms = value * 1000 if value < 1e12 else value
        return _ms_to_iso(ms)
    return None


async def _http_get_json(url: str, headers: Dict[str, str]) -> Tuple[int, Any]:
    """单次 GET 返回 (status, json)。网络/解析异常向上抛，由调用方归一化。"""
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            status = resp.status
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = None
            return status, body


# ── Zhipu GLM ──


def _parse_zhipu_tiers(data: Dict[str, Any]) -> List[QuotaTier]:
    limits = data.get("limits") if isinstance(data.get("limits"), list) else []
    token_limits: List[Tuple[float, float, Optional[str]]] = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").upper() != "TOKENS_LIMIT":
            continue
        percentage = _parse_f64(item.get("percentage")) or 0.0
        reset_ms = _parse_f64(item.get("nextResetTime"))
        if reset_ms is None:
            reset_ms = float("inf")
        reset_iso = None if reset_ms == float("inf") else _ms_to_iso(reset_ms)
        token_limits.append((reset_ms, percentage, reset_iso))
    # Sort by reset time ascending; nearest reset = five_hour
    token_limits.sort(key=lambda t: t[0])

    tiers: List[QuotaTier] = []
    if token_limits:
        nearest = token_limits[0]
        tiers.append(
            {
                "name": "five_hour",
                "label": "5h limit",
                "usedPercent": round(nearest[1], 2),
                "resetsAt": nearest[2],
            }
        )
    if len(token_limits) > 1:
        farthest = token_limits[-1]
        if farthest[0] != token_limits[0][0]:
            tiers.append(
                {
                    "name": "weekly_limit",
                    "label": "Weekly limit",
                    "usedPercent": round(farthest[1], 2),
                    "resetsAt": farthest[2],
                }
            )
    return tiers


def _zhipu_result_from_body(body: Any) -> Optional[ProviderQuotaResult]:
    """成功返回 result；厂商明确报错（success:false）返回带 msg 的失败 result；
    结构不符返回 None（调用方按不可用处理）。"""
    if not isinstance(body, dict):
        return None
    if body.get("success") is False:
        return _make_error("zhipu", "Zhipu GLM", f"API error: {body.get('msg') or 'Unknown error'}")
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    level = data.get("level") if isinstance(data.get("level"), str) else None
    return {
        "provider": "zhipu",
        "providerLabel": "Zhipu GLM",
        "planName": level,
        "tiers": _parse_zhipu_tiers(data),
        "balance": None,
        "success": True,
        "error": None,
    }


async def _query_zhipu(api_key: str) -> ProviderQuotaResult:
    provider, label = "zhipu", "Zhipu GLM"
    try:
        status, body = await _http_get_json(
            "https://api.z.ai/api/monitor/usage/quota/limit",
            {
                "Authorization": api_key,  # No Bearer prefix for Zhipu
                "Content-Type": "application/json",
                "Accept-Language": "en-US,en",
            },
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        result = _zhipu_result_from_body(body)
        if result is None:
            return _make_error(provider, label, "Missing data field")
        return result
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── Kimi ──


def _kimi_tiers_from_body(body: Any) -> Optional[List[QuotaTier]]:
    if not isinstance(body, dict):
        return None
    if not isinstance(body.get("limits"), list) and not isinstance(body.get("usage"), dict):
        return None
    tiers: List[QuotaTier] = []

    # 5h window from limits[]
    limits = body.get("limits") if isinstance(body.get("limits"), list) else []
    for item in limits:
        if not isinstance(item, dict):
            continue
        detail = item.get("detail")
        if not isinstance(detail, dict):
            continue
        limit = _parse_f64(detail.get("limit")) or 1.0
        remaining = _parse_f64(detail.get("remaining")) or 0.0
        used = max(0.0, limit - remaining)
        tiers.append(
            {
                "name": "five_hour",
                "label": "5h limit",
                "usedPercent": round((used / limit) * 100, 2) if limit > 0 else 0.0,
                "resetsAt": _extract_reset_time(detail.get("resetTime")),
            }
        )

    # Weekly from usage
    usage = body.get("usage")
    if isinstance(usage, dict):
        limit = _parse_f64(usage.get("limit")) or 1.0
        remaining = _parse_f64(usage.get("remaining")) or 0.0
        used = max(0.0, limit - remaining)
        tiers.append(
            {
                "name": "weekly_limit",
                "label": "Weekly limit",
                "usedPercent": round((used / limit) * 100, 2) if limit > 0 else 0.0,
                "resetsAt": _extract_reset_time(usage.get("resetTime")),
            }
        )
    return tiers


async def _query_kimi(api_key: str) -> ProviderQuotaResult:
    provider, label = "kimi", "Kimi"
    try:
        status, body = await _http_get_json(
            "https://api.kimi.com/coding/v1/usages",
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        tiers = _kimi_tiers_from_body(body)
        if tiers is None:
            return _make_error(provider, label, "Unexpected quota response schema")
        return {
            "provider": provider,
            "providerLabel": label,
            "planName": None,
            "tiers": tiers,
            "balance": None,
            "success": True,
            "error": None,
        }
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── MiniMax ──


def _minimax_tiers_from_body(body: Any, *, include_weekly: bool) -> Optional[List[QuotaTier]]:
    if not isinstance(body, dict):
        return None
    base_resp = body.get("base_resp")
    if isinstance(base_resp, dict):
        status_code = base_resp.get("status_code")
        # 对齐 connector：仅 number 且非 0 才判业务错误（字符串 "0" 不误伤）
        if isinstance(status_code, (int, float)) and not isinstance(status_code, bool) and status_code != 0:
            return None
    model_remains = body.get("model_remains")
    if not isinstance(model_remains, list) or not model_remains:
        return None
    first = model_remains[0]
    if not isinstance(first, dict):
        return None
    tiers: List[QuotaTier] = []
    interval_total = _parse_f64(first.get("current_interval_total_count")) or 0.0
    interval_used = _parse_f64(first.get("current_interval_usage_count")) or 0.0
    if interval_total > 0:
        end_time = first.get("end_time")
        tiers.append(
            {
                "name": "five_hour",
                "label": "5h limit",
                "usedPercent": round((interval_used / interval_total) * 100, 2),
                "resetsAt": (
                    _ms_to_iso(end_time)
                    if isinstance(end_time, (int, float)) and not isinstance(end_time, bool)
                    else None
                ),
            }
        )
    if include_weekly:
        weekly_total = _parse_f64(first.get("current_weekly_total_count")) or 0.0
        weekly_used = _parse_f64(first.get("current_weekly_usage_count")) or 0.0
        if weekly_total > 0:
            weekly_end = first.get("weekly_end_time")
            tiers.append(
                {
                    "name": "weekly_limit",
                    "label": "Weekly limit",
                    "usedPercent": round((weekly_used / weekly_total) * 100, 2),
                    "resetsAt": (
                        _ms_to_iso(weekly_end)
                        if isinstance(weekly_end, (int, float))
                        and not isinstance(weekly_end, bool)
                        else None
                    ),
                }
            )
    return tiers


async def _query_minimax(api_key: str, is_cn: bool) -> ProviderQuotaResult:
    provider = "minimax_cn" if is_cn else "minimax_en"
    label = "MiniMax"
    domain = "api.minimaxi.com" if is_cn else "api.minimax.io"
    try:
        status, body = await _http_get_json(
            f"https://{domain}/v1/api/openplatform/coding_plan/remains",
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        if isinstance(body, dict):
            base_resp = body.get("base_resp")
            if isinstance(base_resp, dict) and base_resp.get("status_code") not in (None, 0):
                return _make_error(
                    provider,
                    label,
                    f"API error (code {base_resp.get('status_code')}): "
                    f"{base_resp.get('status_msg') or 'Unknown'}",
                )
        tiers = _minimax_tiers_from_body(body, include_weekly=True)
        return {
            "provider": provider,
            "providerLabel": label,
            "planName": None,
            "tiers": tiers or [],
            "balance": None,
            "success": True,
            "error": None,
        }
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── DeepSeek ──


def _deepseek_result_from_body(body: Any) -> Optional[ProviderQuotaResult]:
    if not isinstance(body, dict):
        return None
    infos = body.get("balance_infos")
    first = infos[0] if isinstance(infos, list) and infos else None
    if not isinstance(first, dict):
        return None
    currency = str(first.get("currency") or "CNY")
    return {
        "provider": "deepseek",
        "providerLabel": "DeepSeek",
        "planName": None,
        "tiers": [],
        "balance": {
            "remaining": _parse_f64(first.get("total_balance")) or 0.0,
            "total": None,
            "used": None,
            "unit": currency,
        },
        "success": True,
        "error": None if body.get("is_available") is True else "Insufficient balance",
    }


async def _query_deepseek(api_key: str) -> ProviderQuotaResult:
    provider, label = "deepseek", "DeepSeek"
    try:
        status, body = await _http_get_json(
            "https://api.deepseek.com/user/balance",
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        result = _deepseek_result_from_body(body)
        if result is None:
            return _make_error(provider, label, "No balance info returned")
        return result
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── StepFun ──


def _stepfun_result(balance: float) -> ProviderQuotaResult:
    return {
        "provider": "stepfun",
        "providerLabel": "StepFun",
        "planName": None,
        "tiers": [],
        "balance": {"remaining": balance, "total": None, "used": None, "unit": "CNY"},
        "success": True,
        "error": None,
    }


async def _query_stepfun(api_key: str) -> ProviderQuotaResult:
    provider, label = "stepfun", "StepFun"
    try:
        status, body = await _http_get_json(
            "https://api.stepfun.com/v1/accounts",
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        balance = _parse_f64(body.get("balance") if isinstance(body, dict) else None) or 0.0
        return _stepfun_result(balance)
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── SiliconFlow ──


async def _query_siliconflow(api_key: str, is_cn: bool) -> ProviderQuotaResult:
    provider = "siliconflow_cn" if is_cn else "siliconflow_en"
    label = "SiliconFlow" if is_cn else "SiliconFlow (EN)"
    domain = "api.siliconflow.cn" if is_cn else "api.siliconflow.com"
    unit = "CNY" if is_cn else "USD"
    try:
        status, body = await _http_get_json(
            f"https://{domain}/v1/user/info",
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return _make_error(provider, label, "Missing data field")
        total_balance = _parse_f64(data.get("totalBalance")) or 0.0
        return {
            "provider": provider,
            "providerLabel": label,
            "planName": None,
            "tiers": [],
            "balance": {
                "remaining": total_balance,
                "total": None,
                "used": None,
                "unit": unit,
            },
            "success": True,
            "error": None,
        }
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── OpenRouter ──


def _openrouter_result_from_body(body: Any) -> Optional[ProviderQuotaResult]:
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    if not isinstance(data, dict):
        return None
    total_credits = _parse_f64(data.get("total_credits")) or 0.0
    total_usage = _parse_f64(data.get("total_usage")) or 0.0
    return {
        "provider": "openrouter",
        "providerLabel": "OpenRouter",
        "planName": None,
        "tiers": [],
        "balance": {
            "remaining": total_credits - total_usage,
            "total": total_credits,
            "used": total_usage,
            "unit": "USD",
        },
        "success": True,
        "error": None,
    }


async def _query_openrouter(api_key: str) -> ProviderQuotaResult:
    provider, label = "openrouter", "OpenRouter"
    try:
        status, body = await _http_get_json(
            "https://openrouter.ai/api/v1/credits",
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        result = _openrouter_result_from_body(body)
        if result is None:
            return _make_error(provider, label, "Missing data field")
        return result
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── Novita AI ──


def _novita_result(available: float) -> ProviderQuotaResult:
    return {
        "provider": "novita",
        "providerLabel": "Novita AI",
        "planName": None,
        "tiers": [],
        "balance": {"remaining": available, "total": None, "used": None, "unit": "USD"},
        "success": True,
        "error": None,
    }


async def _query_novita(api_key: str) -> ProviderQuotaResult:
    provider, label = "novita", "Novita AI"
    try:
        status, body = await _http_get_json(
            "https://api.novita.ai/v3/user/balance",
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )
        if status in (401, 403):
            return _make_error(provider, label, f"Authentication failed (HTTP {status})")
        if status != 200:
            return _make_error(provider, label, f"API error (HTTP {status})")
        # Novita balance unit is 0.0001 USD
        raw = _parse_f64(body.get("availableBalance") if isinstance(body, dict) else None)
        return _novita_result((raw or 0.0) / 10000)
    except Exception as exc:
        return _make_error(provider, label, f"Network error: {exc}")


# ── 经 base_url（中转/代理）的探测查询 ──


async def _query_via_base_url(
    provider_id: ProviderId, base_url: str, api_key: str
) -> Optional[ProviderQuotaResult]:
    """Query a provider's quota API through the configured base_url instead of the hardcoded domain."""
    origin = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    try:
        if provider_id == "zhipu":
            status, body = await _http_get_json(
                f"{origin}/api/monitor/usage/quota/limit",
                {
                    "Authorization": api_key,
                    "Content-Type": "application/json",
                    "Accept-Language": "en-US,en",
                },
            )
            if status != 200:
                return None
            # 探测语义：只接受成功结果；厂商明确报错视同本 provider 不可用
            result = _zhipu_result_from_body(body)
            return result if result and result.get("success") else None

        if provider_id == "deepseek":
            status, body = await _http_get_json(f"{origin}/user/balance", headers)
            if status != 200:
                return None
            return _deepseek_result_from_body(body)

        if provider_id == "kimi":
            status, body = await _http_get_json(f"{origin}/coding/v1/usages", headers)
            if status != 200:
                return None
            tiers = _kimi_tiers_from_body(body)
            if tiers is None:
                return None
            return {
                "provider": "kimi",
                "providerLabel": "Kimi",
                "planName": None,
                "tiers": tiers,
                "balance": None,
                "success": True,
                "error": None,
            }

        if provider_id == "openrouter":
            status, body = await _http_get_json(f"{origin}/api/v1/credits", headers)
            if status != 200 or not isinstance(body, dict):
                return None
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            if not isinstance(data, dict):
                return None
            if "total_credits" not in data and "total_usage" not in data:
                return None
            return _openrouter_result_from_body(body)

        if provider_id == "stepfun":
            status, body = await _http_get_json(f"{origin}/v1/accounts", headers)
            if status != 200 or not isinstance(body, dict) or "balance" not in body:
                return None
            return _stepfun_result(_parse_f64(body.get("balance")) or 0.0)

        if provider_id in ("minimax_cn", "minimax_en"):
            status, body = await _http_get_json(
                f"{origin}/v1/api/openplatform/coding_plan/remains",
                {**headers, "Content-Type": "application/json"},
            )
            if status != 200:
                return None
            # 对齐 connector：via-base_url 版本只取 5h 窗口，不取 weekly
            tiers = _minimax_tiers_from_body(body, include_weekly=False)
            if tiers is None:
                return None
            return {
                "provider": provider_id,
                "providerLabel": "MiniMax",
                "planName": None,
                "tiers": tiers,
                "balance": None,
                "success": True,
                "error": None,
            }

        if provider_id in ("siliconflow_cn", "siliconflow_en"):
            status, body = await _http_get_json(f"{origin}/v1/user/info", headers)
            if status != 200 or not isinstance(body, dict):
                return None
            data = body.get("data")
            if not isinstance(data, dict):
                return None
            return {
                "provider": provider_id,
                "providerLabel": "SiliconFlow",
                "planName": None,
                "tiers": [],
                "balance": {
                    "remaining": _parse_f64(data.get("totalBalance")) or 0.0,
                    "total": None,
                    "used": None,
                    "unit": "CNY",
                },
                "success": True,
                "error": None,
            }

        if provider_id == "novita":
            status, body = await _http_get_json(f"{origin}/v3/user/balance", headers)
            if status != 200 or not isinstance(body, dict) or "availableBalance" not in body:
                return None
            return _novita_result((_parse_f64(body.get("availableBalance")) or 0.0) / 10000)
    except Exception:
        return None
    return None


# Shared relays may route different credentials to different providers. Keep
# the cache account-isolated with a one-way fingerprint; never retain raw keys.
_provider_probe_cache: Dict[str, Tuple[ProviderId, float]] = {}
_PROVIDER_PROBE_CACHE_TTL_S = 5 * 60  # 5 minutes
_PROBE_PROVIDER_ORDER: Tuple[ProviderId, ...] = (
    "zhipu",
    "deepseek",
    "kimi",
    "openrouter",
    "stepfun",
    "minimax_cn",
    "siliconflow_cn",
    "novita",
)


async def _probe_provider_via_base_url(
    base_url: str, api_key: str
) -> Optional[ProviderQuotaResult]:
    """Try each provider's quota API through the configured base_url (proxy).
    The proxy forwards to the real provider; whichever returns success identifies the provider."""
    fingerprint = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    cache_key = f"{base_url.rstrip('/').lower()}|{fingerprint}"
    cached = _provider_probe_cache.get(cache_key)
    now = time.time()
    if cached and now - cached[1] <= _PROVIDER_PROBE_CACHE_TTL_S:
        result = await _query_via_base_url(cached[0], base_url, api_key)
        if result:
            return result
        _provider_probe_cache.pop(cache_key, None)
    elif cached:
        _provider_probe_cache.pop(cache_key, None)

    results = await asyncio.gather(
        *(_query_via_base_url(pid, base_url, api_key) for pid in _PROBE_PROVIDER_ORDER),
        return_exceptions=True,
    )
    for idx, result in enumerate(results):
        if isinstance(result, dict) and result.get("success"):
            _provider_probe_cache[cache_key] = (_PROBE_PROVIDER_ORDER[idx], time.time())
            return result
    return None


# ── Public entry point ──


async def query_provider_quota(
    base_url: str,
    api_key: str,
    provider_hint: Optional[str] = None,
) -> ProviderQuotaResult:
    if not api_key.strip():
        return _make_error("unknown", "Unknown", "API key is empty")

    # 1. Fast path: URL-based detection
    hinted_provider = normalize_provider_id(provider_hint)
    info = detect_provider(base_url)
    if info:
        provider_id = info[0]
        if provider_id == "zhipu":
            return await _query_zhipu(api_key)
        if provider_id == "kimi":
            return await _query_kimi(api_key)
        if provider_id == "minimax_cn":
            return await _query_minimax(api_key, True)
        if provider_id == "minimax_en":
            return await _query_minimax(api_key, False)
        if provider_id == "deepseek":
            return await _query_deepseek(api_key)
        if provider_id == "stepfun":
            return await _query_stepfun(api_key)
        if provider_id == "siliconflow_cn":
            return await _query_siliconflow(api_key, True)
        if provider_id == "siliconflow_en":
            return await _query_siliconflow(api_key, False)
        if provider_id == "openrouter":
            return await _query_openrouter(api_key)
        if provider_id == "novita":
            return await _query_novita(api_key)

    # Explicit hints let adapters use opaque relay URLs without exposing the
    # credential to unrelated provider domains.
    if hinted_provider:
        hinted_result = await _query_via_base_url(hinted_provider, base_url, api_key)
        if hinted_result:
            return hinted_result
        return _make_error(
            hinted_provider,
            hinted_provider,
            f"Quota API unavailable through base URL: {base_url}",
        )

    # 2. Probe all providers through the configured base_url (proxy)
    probe_result = await _probe_provider_via_base_url(base_url, api_key)
    if probe_result:
        return probe_result

    return _make_error(
        "unknown", "Unknown", f"Could not identify provider for base URL: {base_url}"
    )


# ── 工具栏展示格式（对齐 presentation.ts 的 providerQuotaToRateLimits） ──


def _reset_epoch_seconds(iso: Optional[str]) -> int:
    if not iso:
        return 0
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def provider_quota_to_rate_limits(
    quota: ProviderQuotaResult, sampled_at_ms: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Adapter-neutral toolbar representation (fiveHour/sevenDay/credit)."""
    if sampled_at_ms is None:
        sampled_at_ms = int(time.time() * 1000)
    tiers = quota.get("tiers") or []
    five_hour = next((t for t in tiers if t.get("name") == "five_hour"), None)
    weekly = next((t for t in tiers if t.get("name") == "weekly_limit"), None)
    if five_hour or weekly:
        result: Dict[str, Any] = {}
        if five_hour:
            result["fiveHour"] = {
                "usedPercentage": five_hour.get("usedPercent", 0),
                "resetsAt": _reset_epoch_seconds(five_hour.get("resetsAt")),
            }
        if weekly:
            result["sevenDay"] = {
                "usedPercentage": weekly.get("usedPercent", 0),
                "resetsAt": _reset_epoch_seconds(weekly.get("resetsAt")),
            }
        result["sampledAt"] = sampled_at_ms
        return result
    balance = quota.get("balance")
    if not balance:
        return None
    return {
        "credit": {
            "remaining": balance.get("remaining"),
            "total": balance.get("total"),
            "used": balance.get("used"),
            "unit": balance.get("unit"),
            "resetsAt": _reset_epoch_seconds(balance.get("resetsAt")),
        },
        "planName": quota.get("planName"),
        "sampledAt": sampled_at_ms,
    }
