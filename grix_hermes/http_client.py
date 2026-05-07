"""Grix HTTP API client for auth, registration, and agent management.

Used when WebSocket credentials are not yet available — the HTTP path handles
initial account setup and API agent creation.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://grix.dhf.pub"
DEFAULT_TIMEOUT_SECONDS = 15


class GrixHttpError(Exception):
    def __init__(self, message: str, status: int = 0, code: int = -1, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _resolve_base_url(explicit: Optional[str] = None) -> str:
    return _clean(explicit) or _clean(os.environ.get("GRIX_WEB_BASE_URL")) or DEFAULT_BASE_URL


def _normalize_base_url(raw: str) -> str:
    base = _clean(raw) or _resolve_base_url()
    parsed = urllib.parse.urlparse(base)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError(f"Invalid base URL: {base}")

    path = (parsed.path or "").rstrip("/")
    if not path:
        path = "/v1"
    elif not path.endswith("/v1"):
        path = f"{path}/v1"

    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _derive_portal_url(raw: str) -> str:
    base = _clean(raw) or _resolve_base_url()
    parsed = urllib.parse.urlparse(base)
    path = (parsed.path or "").rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3]
    return f"{parsed.scheme}://{parsed.netloc}{path}/"


async def _request_json(
    method: str,
    path: str,
    base_url: str,
    body: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    import aiohttp

    api_base = _normalize_base_url(base_url)
    url = f"{api_base}{path}" if path.startswith("/") else f"{api_base}/{path}"

    final_headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body)
        final_headers["Content-Type"] = "application/json"

    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.request(method, url, headers=final_headers, data=data) as resp:
                raw = await resp.text()
                status = resp.status
    except Exception as exc:
        raise GrixHttpError(f"network error: {exc}")

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise GrixHttpError(f"invalid json response: {raw[:256]}", status)

    if not isinstance(payload, dict):
        raise GrixHttpError(f"unexpected response format: {type(payload).__name__}", status)

    code = int(payload.get("code", -1) or -1)
    msg = _clean(payload.get("msg")) or "unknown error"
    if status >= 400 or code != 0:
        raise GrixHttpError(msg, status, code, payload)

    return {"api_base_url": api_base, "status": status, "data": payload.get("data"), "payload": payload}


def _build_auth_result(action: str, result: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    data = result.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    user = data.get("user") or {}
    if not isinstance(user, dict):
        user = {}
    return {
        "ok": True,
        "action": action,
        "api_base_url": result["api_base_url"],
        "access_token": str(data.get("access_token", "")),
        "refresh_token": str(data.get("refresh_token", "")),
        "expires_in": int(data.get("expires_in", 0) or 0),
        "user_id": str(user.get("id", "")),
        "portal_url": _derive_portal_url(base_url),
        "data": data,
    }


def _build_agent_result(action: str, result: Dict[str, Any], is_main: bool) -> Dict[str, Any]:
    data = result.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    agent_id = _clean(data.get("id"))
    api_endpoint = _clean(data.get("api_endpoint"))
    api_key = _clean(data.get("api_key"))
    agent_name = _clean(data.get("agent_name"))

    return {
        "ok": True,
        "action": action,
        "api_base_url": result["api_base_url"],
        "agent_id": agent_id,
        "agent_name": agent_name,
        "is_main": bool(is_main),
        "provider_type": int(data.get("provider_type", 0) or 0),
        "api_endpoint": api_endpoint,
        "api_key": api_key,
        "api_key_hint": _clean(data.get("api_key_hint")),
        "session_id": _clean(data.get("session_id")),
        "data": data,
    }


async def send_email_code(
    email: str,
    scene: str,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    result = await _request_json("POST", "/auth/send-code", _resolve_base_url(base_url), {
        "email": _clean(email),
        "scene": _clean(scene),
    })
    return {"ok": True, "action": "send-email-code", "api_base_url": result["api_base_url"], "data": result["data"]}


async def register(
    email: str,
    password: str,
    email_code: str,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    platform = "web"
    device_id = f"{platform}_{uuid.uuid4().hex}"
    result = await _request_json("POST", "/auth/register", _resolve_base_url(base_url), {
        "email": _clean(email),
        "password": _clean(password),
        "email_code": _clean(email_code),
        "device_id": device_id,
        "platform": platform,
    })
    return _build_auth_result("register", result, _resolve_base_url(base_url))


async def login(
    account: str,
    password: str,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    platform = "web"
    device_id = f"{platform}_{uuid.uuid4().hex}"
    result = await _request_json("POST", "/auth/login", _resolve_base_url(base_url), {
        "account": _clean(account),
        "password": _clean(password),
        "device_id": device_id,
        "platform": platform,
    })
    return _build_auth_result("login", result, _resolve_base_url(base_url))


async def list_agents(
    access_token: str,
    base_url: Optional[str] = None,
) -> List[Dict[str, Any]]:
    result = await _request_json("GET", "/agents/list", _resolve_base_url(base_url), headers={
        "Authorization": f"Bearer {_clean(access_token)}",
    })
    data = result.get("data") or {}
    if isinstance(data, dict):
        items = data.get("list")
    else:
        items = None
    if isinstance(items, list):
        return items
    return []


async def create_api_agent(
    access_token: str,
    agent_name: str,
    is_main: bool = True,
    avatar_url: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "agent_name": _clean(agent_name),
        "provider_type": 3,
        "is_main": bool(is_main),
    }
    cleaned_avatar = _clean(avatar_url)
    if cleaned_avatar:
        body["avatar_url"] = cleaned_avatar

    result = await _request_json("POST", "/agents/create", _resolve_base_url(base_url), body, headers={
        "Authorization": f"Bearer {_clean(access_token)}",
    })
    return _build_agent_result("create-api-agent", result, bool(is_main))


async def rotate_api_key(
    access_token: str,
    agent_id: str,
    is_main: bool = True,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    result = await _request_json(
        "POST",
        f"/agents/{_clean(agent_id)}/api/key/rotate",
        _resolve_base_url(base_url),
        {},
        headers={"Authorization": f"Bearer {_clean(access_token)}"},
    )
    return _build_agent_result("rotate-api-key", result, bool(is_main))


async def create_or_reuse_agent(
    access_token: str,
    agent_name: str,
    is_main: bool = True,
    prefer_existing: bool = True,
    rotate_on_reuse: bool = True,
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    if prefer_existing:
        agents = await list_agents(access_token, base_url)
        for item in agents:
            if not isinstance(item, dict):
                continue
            if _clean(item.get("agent_name")) != _clean(agent_name):
                continue
            if int(item.get("provider_type", 0) or 0) != 3:
                continue
            if int(item.get("status", 0) or 0) == 3:
                continue
            if not rotate_on_reuse:
                raise GrixHttpError(
                    "existing agent found but rotate-on-reuse is disabled",
                    payload={"existing_agent": item},
                )
            rotated = await rotate_api_key(access_token, _clean(item.get("id")), is_main, base_url)
            rotated["source"] = "reused_existing_agent_with_rotated_key"
            return rotated

    created = await create_api_agent(access_token, agent_name, is_main, base_url=base_url)
    created["source"] = "created_new_agent"
    return created
