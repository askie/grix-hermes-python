"""进程级配额查询缓存（平移自 connector src/core/provider-quota/service.ts）。

Cache entries are isolated by provider, endpoint, account identity and a
one-way credential fingerprint. Concurrent requests for the same account
share one in-flight HTTP query.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any, Callable, Dict, Optional, TypedDict
from urllib.parse import urlparse

from .provider_quota import (
    ProviderQuotaResult,
    ProviderQuotaSource,
    detect_provider,
    normalize_provider_id,
    query_provider_quota,
)


class ProviderQuotaSnapshot(TypedDict):
    quota: ProviderQuotaResult
    sampledAt: int  # epoch ms
    cached: bool
    cacheKey: str


def resolve_quota_base_url(source: ProviderQuotaSource) -> str:
    explicit = (source.get("quotaBaseUrl") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    inference = (source.get("baseUrl") or "").strip().rstrip("/")
    if detect_provider(inference):
        return inference
    try:
        parsed = urlparse(inference)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except ValueError:
        pass
    return inference


class ProviderQuotaService:
    def __init__(
        self,
        ttl_ms: int = 60_000,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self._ttl_ms = ttl_ms
        self._now = now or (lambda: time.time() * 1000)
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._in_flight: Dict[str, asyncio.Task] = {}

    def cache_key(self, source: ProviderQuotaSource) -> str:
        provider_id = (
            normalize_provider_id(source.get("providerId"))
            or (detect_provider(source.get("baseUrl") or "") or (None,))[0]
            or "unknown"
        )
        endpoint = resolve_quota_base_url(source).lower()
        account = (source.get("accountKey") or "").strip() or "default"
        fingerprint = hashlib.sha256((source.get("apiKey") or "").encode()).hexdigest()[:16]
        return f"{provider_id}|{endpoint}|{account}|{fingerprint}"

    async def query(
        self, source: ProviderQuotaSource, *, fresh: bool = False
    ) -> ProviderQuotaSnapshot:
        key = self.cache_key(source)
        now = self._now()
        cached = self._cache.get(key)
        if not fresh and cached and now - cached["sampledAt"] <= self._ttl_ms:
            return {**cached, "cached": True}

        active = self._in_flight.get(key)
        if active is not None:
            snapshot = await active
            return {**snapshot, "cached": False}

        async def _run() -> Dict[str, Any]:
            provider_id = (
                normalize_provider_id(source.get("providerId"))
                or (detect_provider(source.get("baseUrl") or "") or (None,))[0]
                or None
            )
            quota = await query_provider_quota(
                resolve_quota_base_url(source),
                source.get("apiKey") or "",
                provider_id,
            )
            snapshot = {"quota": quota, "sampledAt": self._now(), "cacheKey": key}
            if quota.get("success"):
                self._cache[key] = snapshot
            return snapshot

        task = asyncio.ensure_future(_run())
        self._in_flight[key] = task
        try:
            snapshot = await task
            return {**snapshot, "cached": False}
        finally:
            self._in_flight.pop(key, None)

    def invalidate(self, source: Optional[ProviderQuotaSource] = None) -> None:
        if source is None:
            self._cache.clear()
            return
        self._cache.pop(self.cache_key(source), None)


shared_provider_quota_service = ProviderQuotaService()
