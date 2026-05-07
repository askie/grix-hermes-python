"""Grix egg step executors.

Seven steps for agent incubation:
  detect → install → create → bind → soul → gateway → accept
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

from .egg_state import (
    StateFile,
    iso_now,
    mark_step_done,
    mark_step_failed,
    mark_step_skipped,
    step_is_done,
)

logger = logging.getLogger(__name__)

PLUGIN_NAME = "grix-hermes"
PLUGIN_GIT_REPO = "askie/grix-hermes-python"

PROFILE_NAME_RE = re.compile(r"^(default|[a-z0-9][a-z0-9_-]{0,63})$")
LLM_KEY_RE = re.compile(r"^(?:.*_)?(?:API_KEY|BASE_URL|MODEL|URL)$")
LLM_CONFIG_KEYS = ["model", "custom_providers", "providers", "fallback_providers", "smart_model_routing", "auxiliary"]
RESTRICTED_MANAGEMENT_SKILLS = ["grix-admin", "grix-register", "grix-update", "grix-egg"]


def _is_managed_grix_path(candidate: str) -> bool:
    base = os.path.basename(candidate)
    return base.startswith(PLUGIN_NAME) or os.path.isfile(os.path.join(candidate, "grix_hermes", "__init__.py"))


def _read_env_file(path: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not os.path.isfile(path):
        return result
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            result[key.strip()] = value.strip()
    return result


class EggError(Exception):
    def __init__(self, step: str, step_number: int, reason: str, suggestion: str, raw_error: str = ""):
        super().__init__(reason)
        self.step = step
        self.step_number = step_number
        self.suggestion = suggestion
        self.raw_error = raw_error


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _expand_home(value: str) -> str:
    if not value:
        return value
    if value == "~":
        return os.path.expanduser("~")
    if value.startswith("~/"):
        return os.path.join(os.path.expanduser("~"), value[2:])
    return value


def _resolve_hermes_home(explicit: str = "") -> str:
    return os.path.abspath(_expand_home(_clean(explicit) or os.environ.get("HERMES_HOME", "") or "~/.hermes"))


def _resolve_profile_root(hermes_home: str) -> str:
    current = os.path.abspath(hermes_home)
    while os.path.basename(os.path.dirname(current)) == "profiles":
        current = os.path.dirname(os.path.dirname(current))
    return current


def _resolve_profile_dir(hermes_home: str, profile_name: str) -> str:
    name = _clean(profile_name)
    if not name or name == "default":
        return hermes_home
    return os.path.abspath(os.path.join(hermes_home, "profiles", name))


def _default_install_dir(hermes_home: str) -> str:
    return os.path.join(_resolve_profile_root(hermes_home), "plugins", PLUGIN_NAME)


def _plugin_installed(profile_dir: str) -> bool:
    target = os.path.join(profile_dir, "plugins", PLUGIN_NAME)
    return os.path.isdir(target) and os.path.isfile(os.path.join(target, "plugin.yaml"))


def _run_plugins_install(hermes_bin: str, profile_name: str, hermes_home: str) -> None:
    """Install grix-hermes plugin via hermes CLI. Updates if already installed."""
    profile_root = _resolve_profile_root(hermes_home)
    env = {"HERMES_HOME": profile_root}

    # Try update first (plugin may already exist in this profile)
    cmd_prefix = [hermes_bin]
    if profile_name and profile_name != "default":
        cmd_prefix += ["--profile", profile_name]

    code, stdout, stderr = _run_command(
        cmd_prefix + ["plugins", "update", PLUGIN_NAME],
        env=env, check=False,
    )
    if code == 0:
        return

    # Install from git repo
    _run_command(
        cmd_prefix + ["plugins", "install", PLUGIN_GIT_REPO, "--enable"],
        env=env, check=True, timeout=120,
    )


def _is_valid_profile_name(name: str) -> bool:
    return bool(PROFILE_NAME_RE.match(_clean(name)))


def _ensure_private_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private_file(path: str, content: str) -> None:
    _ensure_private_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _run_command(
    cmd: List[str],
    *,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    input_text: Optional[str] = None,
    check: bool = True,
    timeout: int = 60,
) -> Tuple[int, str, str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=merged_env,
            cwd=cwd,
            input=input_text,
            timeout=timeout,
        )
        code = result.returncode
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
    except FileNotFoundError:
        raise EggError("", 0, f"Command not found: {cmd[0]}", f"Install {cmd[0]} or check PATH")
    except subprocess.TimeoutExpired:
        raise EggError("", 0, f"Command timed out: {' '.join(cmd)}", "Check if the process is hanging")

    if check and code != 0:
        raise RuntimeError(stderr or stdout or f"command failed: {' '.join(cmd)}")
    return code, stdout, stderr


def _parse_json(text: str) -> Dict[str, Any]:
    text = _clean(text)
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def _extract_nested(payload: Dict[str, Any], key: str) -> Dict[str, Any]:
    val = payload.get(key)
    if isinstance(val, dict):
        return val
    return {}


# ---------------------------------------------------------------------------
# Step 1: Detect
# ---------------------------------------------------------------------------

def step_detect(
    state: StateFile,
    *,
    route: str = "create_new",
    agent_id: str = "",
    api_endpoint: str = "",
    api_key: str = "",
    bind_json: str = "",
    access_token: str = "",
    email: str = "",
    account: str = "",
    password: str = "",
    hermes_home: str = "",
    profile_name: str = "",
) -> None:
    if step_is_done(state, "detect"):
        return

    route = _clean(route) or "create_new"
    if route not in ("create_new", "existing"):
        raise EggError("detect", 1, f"不支持的路由: {route}", "使用 create_new 或 existing")

    if route == "existing":
        if not bind_json and not (agent_id and api_endpoint and api_key):
            raise EggError(
                "detect", 1,
                "existing 路由需要提供 agent_id、api_endpoint、api_key，或 bind_json",
                "提供完整的已有 agent 凭证",
            )
        state.path = "existing"
        mark_step_done(state, "detect", {"path": "existing"})
        return

    # Check if current session has WS credentials (host path)
    ws_endpoint = os.environ.get("GRIX_ENDPOINT", "").strip()
    ws_agent_id = os.environ.get("GRIX_AGENT_ID", "").strip()
    ws_api_key = os.environ.get("GRIX_API_KEY", "").strip()

    # Also check profile .env file
    if not (ws_endpoint and ws_agent_id and ws_api_key):
        resolved_home = _resolve_hermes_home(hermes_home)
        pname = _clean(profile_name) or state.agent_name
        profile_env = os.path.join(_resolve_profile_dir(_resolve_profile_root(resolved_home), pname), ".env")
        if os.path.isfile(profile_env):
            env_vars = _read_env_file(profile_env)
            ws_endpoint = ws_endpoint or env_vars.get("GRIX_ENDPOINT", "")
            ws_agent_id = ws_agent_id or env_vars.get("GRIX_AGENT_ID", "")
            ws_api_key = ws_api_key or env_vars.get("GRIX_API_KEY", "")

    if ws_endpoint and ws_agent_id and ws_api_key:
        state.path = "host"
        mark_step_done(state, "detect", {"path": "host", "transport": "host_grix_session"})
        return

    # HTTP fallback
    if access_token or email or account:
        state.path = "http"
        mark_step_done(state, "detect", {"path": "http", "transport": "http_login_token"})
        return

    raise EggError(
        "detect", 1,
        "未检测到可用的 Grix 宿主会话凭证，也没有 HTTP 登录信息",
        "请先确保当前 Hermes 已连接 Grix，或提供 access_token / 账号密码用于 HTTP 登录",
    )


# ---------------------------------------------------------------------------
# Step 2: Install (copy grix-hermes to install dir)
# ---------------------------------------------------------------------------

def step_install(
    state: StateFile,
    *,
    hermes_home: str = "",
    install_dir: str = "",
    source_dir: str = "",
    hermes_bin: str = "hermes",
) -> None:
    if step_is_done(state, "install"):
        return

    hermes_home = _resolve_hermes_home(hermes_home)
    profile_root = _resolve_profile_root(hermes_home)
    pname = state.profile_name or state.agent_name
    profile_dir = _resolve_profile_dir(profile_root, pname)

    # If plugin already installed and valid, skip
    if _plugin_installed(profile_dir):
        mark_step_done(state, "install", {"install_dir": _default_install_dir(hermes_home), "skipped": "already_installed"})
        return

    # Use hermes CLI to install from git repo
    _run_plugins_install(hermes_bin, pname, hermes_home)

    mark_step_done(state, "install", {"install_dir": _default_install_dir(hermes_home)})


# ---------------------------------------------------------------------------
# Step 3: Create agent
# ---------------------------------------------------------------------------

async def step_create(
    state: StateFile,
    *,
    agent_name: str = "",
    is_main: bool = True,
    access_token: str = "",
    email: str = "",
    account: str = "",
    password: str = "",
    base_url: str = "",
    category_name: str = "",
    avatar_url: str = "",
    agent_id: str = "",
    api_endpoint: str = "",
    api_key: str = "",
    bind_json: str = "",
    hermes_home: str = "",
) -> Optional[Dict[str, str]]:
    """Returns credentials dict if available after this step."""
    if step_is_done(state, "create"):
        return _credentials_from_state(state, agent_id=agent_id, api_endpoint=api_endpoint, api_key=api_key)

    detected_path = state.path or (state.steps.get("detect", None) and state.steps["detect"].result or {}).get("path", "")

    if detected_path == "existing":
        creds = _resolve_existing_credentials(agent_id, api_endpoint, api_key, bind_json)
        mark_step_done(state, "create", {
            "path": "existing",
            "agent_id": creds["agent_id"],
            "agent_name": creds.get("agent_name", agent_name),
            "api_endpoint": creds["api_endpoint"],
            "api_key": "ak_***",
        })
        return creds

    if detected_path == "host":
        creds = await _create_via_ws(state, agent_name=agent_name, is_main=is_main, category_name=category_name)
        return creds

    # HTTP path
    creds = await _create_via_http(
        state,
        agent_name=agent_name,
        is_main=is_main,
        access_token=access_token,
        email=email,
        account=account,
        password=password,
        base_url=base_url,
        category_name=category_name,
        avatar_url=avatar_url,
    )
    return creds


async def _create_via_ws(
    state: StateFile,
    *,
    agent_name: str,
    is_main: bool = True,
    category_name: str = "",
) -> Dict[str, str]:
    from .invoke_tool import SUPPORTED_ACTIONS
    if "agent_api_create" not in SUPPORTED_ACTIONS:
        raise EggError("create", 3, "当前运行时不支持 agent_api_create", "检查 grix 插件是否正确加载")

    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform
        runner = _gateway_runner_ref()
        if not runner:
            raise EggError("create", 3, "Gateway 未运行", "启动 hermes gateway")
        adapter = runner.adapters.get(Platform("grix"))
        if not adapter:
            raise EggError("create", 3, "Grix adapter 未连接", "检查 Grix 连接")

        result = await adapter.agent_invoke(
            action="agent_api_create",
            params={"agent_name": _clean(agent_name), "provider_type": 3, "is_main": is_main},
            timeout_ms=15000,
        )
    except EggError:
        raise
    except Exception as exc:
        raise EggError("create", 3, f"WS 创建 agent 失败: {exc}", "检查 WS 连接和 GRIX_API_KEY")

    if isinstance(result, str):
        result = _parse_json(result)
    data = result.get("data") or result
    agent_id = _clean(data.get("agent_id") or data.get("id"))
    api_endpoint = _clean(data.get("api_endpoint") or data.get("endpoint"))
    api_key = _clean(data.get("api_key"))
    resolved_name = _clean(data.get("agent_name") or data.get("name") or agent_name)

    if not agent_id or not api_endpoint or not api_key:
        raise EggError(
            "create", 3,
            f"WS 创建未返回完整凭证: agent_id={agent_id}, api_endpoint={api_endpoint}",
            "检查 WS 连接和 agent_api_create 接口",
        )

    mark_step_done(state, "create", {
        "path": "host",
        "agent_id": agent_id,
        "agent_name": resolved_name,
        "api_endpoint": api_endpoint,
        "api_key": "ak_***",
        "transport": "host_grix_session",
    })
    return {"agent_id": agent_id, "agent_name": resolved_name, "api_endpoint": api_endpoint, "api_key": api_key}


async def _create_via_http(
    state: StateFile,
    *,
    agent_name: str,
    is_main: bool = True,
    access_token: str = "",
    email: str = "",
    account: str = "",
    password: str = "",
    base_url: str = "",
    category_name: str = "",
    avatar_url: str = "",
) -> Dict[str, str]:
    from . import http_client

    token = _clean(access_token)
    if not token:
        acct = _clean(email) or _clean(account)
        pwd = _clean(password)
        if not acct or not pwd:
            raise EggError(
                "create", 3,
                "HTTP 创建需要 access_token 或 账号+密码",
                "提供邮箱/账号和密码进行登录，或直接提供 access_token",
            )
        try:
            login_result = await http_client.login(acct, pwd, base_url=base_url or None)
            token = login_result.get("access_token", "")
        except http_client.GrixHttpError as exc:
            raise EggError("create", 3, f"登录失败: {exc}", "检查账号密码是否正确")

    if not token:
        raise EggError("create", 3, "未获取到 access_token", "检查登录流程")

    try:
        result = await http_client.create_or_reuse_agent(
            token,
            agent_name=_clean(agent_name),
            is_main=is_main,
            base_url=base_url or None,
        )
    except http_client.GrixHttpError as exc:
        raise EggError("create", 3, f"HTTP 创建 agent 失败: {exc}", "检查 access_token 和网络连通性")

    agent_id = result.get("agent_id", "")
    api_endpoint = result.get("api_endpoint", "")
    api_key = result.get("api_key", "")
    resolved_name = result.get("agent_name", agent_name)

    if not agent_id or not api_endpoint:
        raise EggError("create", 3, f"HTTP 创建未返回有效凭证: {result}", "检查 HTTP API 是否正常")

    mark_step_done(state, "create", {
        "path": "http",
        "agent_id": agent_id,
        "agent_name": resolved_name,
        "api_endpoint": api_endpoint,
        "api_key": "ak_***",
        "transport": "http_login_token",
    })
    return {"agent_id": agent_id, "agent_name": resolved_name, "api_endpoint": api_endpoint, "api_key": api_key}


def _resolve_existing_credentials(
    agent_id: str,
    api_endpoint: str,
    api_key: str,
    bind_json: str,
) -> Dict[str, str]:
    bind_json = _clean(bind_json)
    if bind_json:
        try:
            payload = json.loads(bind_json)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EggError("create", 3, f"bind_json 解析失败: {exc}", "检查 JSON 格式")
        handoff = _extract_nested(payload, "handoff")
        bind_hermes = _extract_nested(handoff, "bind_hermes")
        source = bind_hermes if bind_hermes else _extract_nested(handoff, "bind_local")
        if not source:
            source = _extract_nested(payload, "createdAgent")
        if not source:
            source = payload
        agent_id = _clean(source.get("agent_id") or source.get("id") or agent_id)
        api_endpoint = _clean(source.get("api_endpoint") or source.get("endpoint") or api_endpoint)
        api_key = _clean(source.get("api_key") or api_key)

    agent_id = _clean(agent_id)
    api_endpoint = _clean(api_endpoint)
    api_key = _clean(api_key)
    if not agent_id or not api_endpoint or not api_key:
        raise EggError(
            "create", 3,
            "existing 路由需要完整的 agent_id、api_endpoint 和 api_key",
            "提供完整凭证或 bind_json",
        )
    return {"agent_id": agent_id, "api_endpoint": api_endpoint, "api_key": api_key}


def _credentials_from_state(
    state: StateFile,
    *,
    agent_id: str = "",
    api_endpoint: str = "",
    api_key: str = "",
) -> Dict[str, str]:
    create_result = (state.steps.get("create") or None) and state.steps["create"].result or {}
    return {
        "agent_id": _clean(create_result.get("agent_id") or agent_id),
        "agent_name": _clean(create_result.get("agent_name") or state.agent_name),
        "api_endpoint": _clean(create_result.get("api_endpoint") or api_endpoint),
        "api_key": _clean(api_key),
    }


# ---------------------------------------------------------------------------
# Step 4: Bind (create profile + write .env + patch config.yaml)
# ---------------------------------------------------------------------------

def step_bind(
    state: StateFile,
    *,
    credentials: Optional[Dict[str, str]] = None,
    hermes_home: str = "",
    profile_name: str = "",
    agent_name: str = "",
    is_main: str = "true",
    account_id: str = "",
    allowed_users: str = "",
    allow_all_users: str = "",
    home_channel: str = "",
    home_channel_name: str = "",
    install_dir: str = "",
    hermes_bin: str = "hermes",
    inherit_keys: bool = True,
) -> None:
    if step_is_done(state, "bind"):
        return

    hermes_home = _resolve_hermes_home(hermes_home)
    profile_root = _resolve_profile_root(hermes_home)
    pname = _clean(profile_name) or state.profile_name or _clean(agent_name)
    profile_dir = _resolve_profile_dir(profile_root, pname)
    inst_dir = _clean(install_dir) or _default_install_dir(hermes_home)
    create_result = (state.steps.get("create") or None) and state.steps["create"].result or {}

    # Extract credentials
    creds = credentials or {}
    final_agent_id = _clean(creds.get("agent_id") or create_result.get("agent_id"))
    final_endpoint = _clean(creds.get("api_endpoint") or create_result.get("api_endpoint"))
    final_api_key = _clean(creds.get("api_key"))
    final_agent_name = _clean(creds.get("agent_name") or create_result.get("agent_name") or agent_name)

    if not final_agent_id or not final_endpoint or not final_api_key:
        raise EggError(
            "bind", 4,
            "绑定需要 agent_id、api_endpoint 和 api_key",
            "检查 create 步骤是否成功返回了完整凭证",
        )

    # Create profile if needed
    profile_created = False
    if not os.path.isdir(profile_dir):
        try:
            _run_command([hermes_bin, "profile", "create", pname], env={"HERMES_HOME": profile_root})
            profile_created = True
        except Exception as exc:
            raise EggError("bind", 4, f"创建 profile 失败: {exc}", f"手动运行: hermes profile create {pname}")
        # Ensure blank state files
        _ensure_blank_profile(profile_dir)

    # Write .env
    env_path = os.path.join(profile_dir, ".env")
    env_updates = {
        "GRIX_ENDPOINT": final_endpoint,
        "GRIX_AGENT_ID": final_agent_id,
        "GRIX_API_KEY": final_api_key,
    }
    if _clean(account_id):
        env_updates["GRIX_ACCOUNT_ID"] = _clean(account_id)
    env_removals: set = set()
    if _clean(allowed_users):
        env_updates["GRIX_ALLOWED_USERS"] = _clean(allowed_users)
        env_removals.add("GRIX_ALLOW_ALL_USERS")
        env_removals.add("GATEWAY_ALLOW_ALL_USERS")
    elif _clean(allow_all_users).lower() == "true":
        env_updates["GRIX_ALLOW_ALL_USERS"] = "true"
        env_updates["GATEWAY_ALLOW_ALL_USERS"] = "true"
        env_removals.add("GRIX_ALLOWED_USERS")
    if _clean(home_channel):
        env_updates["GRIX_HOME_CHANNEL"] = _clean(home_channel)
    if _clean(home_channel_name):
        env_updates["GRIX_HOME_CHANNEL_NAME"] = _clean(home_channel_name)
    _apply_env(env_path, env_updates, env_removals)

    # Patch config.yaml
    config_path = os.path.join(profile_dir, "config.yaml")
    management_policy = _resolve_management_policy(os.path.isdir(profile_dir) and not profile_created, is_main)
    _patch_config(config_path, management_policy, final_endpoint)

    # Inherit LLM keys
    inherited_keys: List[str] = []
    if inherit_keys:
        inherited_keys = _inherit_llm_keys(hermes_home, env_path)
        inherited_config = _inherit_llm_config(hermes_home, config_path)

    state.profile_name = pname
    mark_step_done(state, "bind", {
        "profile_name": pname,
        "profile_dir": profile_dir,
        "env_path": env_path,
        "config_path": config_path,
        "profile_created": profile_created,
    })


def _ensure_blank_profile(profile_dir: str) -> None:
    for sub in ["memories"]:
        _ensure_private_dir(os.path.join(profile_dir, sub))
    for name, content in [("SOUL.md", ""), ("memories/USER.md", ""), ("memories/MEMORY.md", "")]:
        p = os.path.join(profile_dir, name)
        if not os.path.exists(p):
            _write_private_file(p, content)


def _apply_env(env_path: str, updates: Dict[str, str], removals: Optional[set] = None) -> None:
    lines: List[str] = []
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")

    to_remove = removals or set()
    seen = set()
    result: List[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            result.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in to_remove:
            continue
        if key in updates:
            result.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            result.append(line)
    for key, value in updates.items():
        if key not in seen:
            result.append(f"{key}={value}")

    _write_private_file(env_path, "\n".join(result).rstrip() + "\n")


def _resolve_management_policy(profile_exists: bool, is_main: str) -> str:
    val = _clean(is_main).lower()
    if val == "true":
        return "main"
    if val == "false":
        return "restricted"
    return "preserve" if profile_exists else "restricted"


def _patch_config(
    config_path: str,
    management_policy: str,
    ws_url: str,
) -> None:
    """Patch hermes config.yaml with grix channel and skill settings."""
    config: Dict[str, Any] = {}
    if os.path.isfile(config_path):
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                parsed = yaml.safe_load(f.read())
            if isinstance(parsed, dict):
                config = parsed
        except Exception:
            pass

    skills = config.get("skills")
    if not isinstance(skills, dict):
        skills = {}
    config["skills"] = skills

    # External dirs — clean up stale grix-hermes paths (plugins manage their own)
    ext_dirs = skills.get("external_dirs") or []
    if isinstance(ext_dirs, str):
        ext_dirs = [e.strip() for e in ext_dirs.split(",") if e.strip()]
    if not isinstance(ext_dirs, list):
        ext_dirs = []
    ext_dirs = [e for e in ext_dirs if not _is_managed_grix_path(e)]
    skills["external_dirs"] = ext_dirs

    # Management skills
    disabled = skills.get("disabled") or []
    if isinstance(disabled, str):
        disabled = [e.strip() for e in disabled.split(",") if e.strip()]
    if not isinstance(disabled, list):
        disabled = []
    if management_policy == "restricted":
        for s in RESTRICTED_MANAGEMENT_SKILLS:
            if s not in disabled:
                disabled.append(s)
    elif management_policy == "main":
        disabled = [s for s in disabled if s not in RESTRICTED_MANAGEMENT_SKILLS]
    skills["disabled"] = disabled

    # Channel config
    if ws_url:
        channels = config.get("channels")
        if not isinstance(channels, dict):
            channels = {}
        config["channels"] = channels
        grix = channels.get("grix")
        if not isinstance(grix, dict):
            grix = {}
        grix["wsUrl"] = ws_url
        channels["grix"] = grix

    # Plugin enablement
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        plugins = {}
    config["plugins"] = plugins

    enabled_plugins = plugins.get("enabled") or []
    if isinstance(enabled_plugins, str):
        enabled_plugins = [e.strip() for e in enabled_plugins.split(",") if e.strip()]
    if not isinstance(enabled_plugins, list):
        enabled_plugins = []
    if PLUGIN_NAME not in enabled_plugins:
        enabled_plugins.append(PLUGIN_NAME)
    plugins["enabled"] = enabled_plugins

    _ensure_private_dir(os.path.dirname(config_path))
    try:
        import yaml
        _write_private_file(config_path, yaml.dump(config, default_flow_style=False, allow_unicode=True))
    except ImportError:
        _write_private_file(config_path, json.dumps(config, indent=2, ensure_ascii=False))


def _inherit_llm_keys(hermes_home: str, target_env_path: str) -> List[str]:
    source_env = os.path.join(hermes_home, ".env")
    if not os.path.isfile(source_env):
        return []
    llm_entries: Dict[str, str] = {}
    with open(source_env, "r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            key = key.strip()
            value = value.strip()
            if LLM_KEY_RE.match(key) and not key.startswith("GRIX_") and value and "***" not in value:
                llm_entries[key] = value
    if not llm_entries:
        return []
    _apply_env(target_env_path, llm_entries)
    return sorted(llm_entries.keys())


def _inherit_llm_config(hermes_home: str, target_config_path: str) -> List[str]:
    source_config_path = os.path.join(hermes_home, "config.yaml")
    if not os.path.isfile(source_config_path):
        return []
    try:
        import yaml
    except ImportError:
        return []
    with open(source_config_path, "r", encoding="utf-8") as f:
        source_config = yaml.safe_load(f.read()) or {}
    if not isinstance(source_config, dict):
        return []
    target_config: Dict[str, Any] = {}
    if os.path.isfile(target_config_path):
        with open(target_config_path, "r", encoding="utf-8") as f:
            parsed = yaml.safe_load(f.read())
        if isinstance(parsed, dict):
            target_config = parsed
    inherited = []
    for key in LLM_CONFIG_KEYS:
        val = source_config.get(key)
        if val is not None and key not in target_config:
            target_config[key] = val
            inherited.append(key)
    if inherited:
        _write_private_file(target_config_path, yaml.dump(target_config, default_flow_style=False, allow_unicode=True))
    return inherited


# ---------------------------------------------------------------------------
# Step 5: Soul
# ---------------------------------------------------------------------------

def step_soul(
    state: StateFile,
    *,
    soul_content: str = "",
    soul_file: str = "",
    hermes_home: str = "",
) -> None:
    if step_is_done(state, "soul"):
        return

    content = _clean(soul_content)
    if not content:
        sf = _clean(soul_file)
        if sf and os.path.isfile(sf):
            with open(sf, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            mark_step_skipped(state, "soul")
            return

    profile_dir = _resolve_profile_dir(
        _resolve_profile_root(_resolve_hermes_home(hermes_home)),
        state.profile_name,
    )
    target = os.path.join(profile_dir, "SOUL.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")

    mark_step_done(state, "soul", {"soul_path": target})


# ---------------------------------------------------------------------------
# Step 6: Gateway
# ---------------------------------------------------------------------------

def step_gateway(
    state: StateFile,
    *,
    hermes_home: str = "",
    hermes_bin: str = "hermes",
) -> None:
    if step_is_done(state, "gateway"):
        return

    hermes_home = _resolve_hermes_home(hermes_home)
    profile_root = _resolve_profile_root(hermes_home)
    pname = state.profile_name
    profile_dir = _resolve_profile_dir(profile_root, pname)
    env = {"HERMES_HOME": profile_root}
    cmd_prefix = [hermes_bin]
    if pname and pname != "default":
        cmd_prefix += ["--profile", pname]

    # Pre-check: verify profile has grix config
    _assert_grix_profile_configured(profile_dir)

    # Check status
    status_cmd = cmd_prefix + ["gateway", "status"]
    code, stdout, stderr = _run_command(status_cmd, env=env, check=False)
    already_running = _is_gateway_running(stdout, stderr)

    if not already_running:
        # Try service start
        start_cmd = cmd_prefix + ["gateway", "start"]
        code, stdout, stderr = _run_command(start_cmd, env=env, check=False)
        if code != 0:
            # Try install + start
            _run_command(cmd_prefix + ["gateway", "install"], env=env, check=False)
            code, stdout, stderr = _run_command(start_cmd, env=env, check=False)

        if code != 0:
            # Fallback: detached manual run
            run_cmd = cmd_prefix + ["gateway", "run"]
            try:
                subprocess.Popen(run_cmd, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    # Wait for running (up to 20s)
    for _ in range(20):
        code, stdout, stderr = _run_command(status_cmd, env=env, check=False)
        if _is_gateway_running(stdout, stderr):
            break
        time.sleep(1)

    # Wait for grix connection in logs (up to 15s)
    _wait_for_grix_connected(profile_dir)

    mark_step_done(state, "gateway", {"profile_name": pname, "already_running": already_running})


def _is_gateway_running(stdout: str, stderr: str) -> bool:
    combined = (stdout + " " + stderr).lower()
    positive = ["running", "healthy", "installed and running"]
    negative = ["not running", "not installed", "inactive", "stopped"]
    if any(n in combined for n in negative):
        return False
    return any(p in combined for p in positive)


def _assert_grix_profile_configured(profile_dir: str) -> None:
    config_path = os.path.join(profile_dir, "config.yaml")
    env_path = os.path.join(profile_dir, ".env")
    if not os.path.isfile(config_path):
        raise EggError("gateway", 6, f"Profile config missing: {config_path}", "Run bind step first")
    if not os.path.isfile(env_path):
        raise EggError("gateway", 6, f"Profile env missing: {env_path}", "Run bind step first")
    with open(env_path, "r") as f:
        env_text = f.read()
    for key in ["GRIX_ENDPOINT", "GRIX_AGENT_ID", "GRIX_API_KEY"]:
        if not re.search(rf"^{key}=\S+", env_text, re.MULTILINE):
            raise EggError("gateway", 6, f"Profile env missing {key}: {env_path}", "Run bind step first")


def _wait_for_grix_connected(profile_dir: str, timeout: float = 15.0) -> None:
    connected_hints = ["[grix] connected to", "grix connected", "grix reconnected successfully"]
    unhealthy_hints = ["no messaging platforms enabled", "grix disabled", "grix platform disabled"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        log_text = _tail_file(os.path.join(profile_dir, "logs", "gateway.log"), 120)
        error_text = _tail_file(os.path.join(profile_dir, "logs", "gateway.error.log"), 120)
        combined = (log_text + "\n" + error_text).lower()

        last_connected = max(combined.find(h) for h in connected_hints)
        last_unhealthy = max(combined.find(h) for h in unhealthy_hints)

        if last_connected >= 0 and last_connected > last_unhealthy:
            return
        if last_unhealthy >= 0 and last_unhealthy > last_connected:
            hint = next((h for h in unhealthy_hints if h in combined), "")
            raise EggError(
                "gateway", 6,
                f"Gateway started but grix platform not healthy: {hint}",
                "检查 config.yaml 中 grix 配置和 .env 中凭证",
            )
        time.sleep(1)
    # Timeout is not fatal — the gateway may still be connecting
    logger.warning("grix connection not confirmed in logs within %.0fs", timeout)


def _tail_file(path: str, max_lines: int) -> str:
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Step 7: Accept (create test group, send probe, verify response)
# ---------------------------------------------------------------------------

async def step_accept(
    state: StateFile,
    *,
    hermes_home: str = "",
    hermes_bin: str = "hermes",
    probe_message: str = "probe",
    expected_substring: str = "",
    accept_timeout: float = 15.0,
    member_ids: str = "",
    member_types: str = "",
    delivery_target: str = "",
    delivery_source: str = "",
) -> None:
    if step_is_done(state, "accept"):
        return

    create_result = (state.steps.get("create") or None) and state.steps["create"].result or {}
    target_agent_id = _clean(create_result.get("agent_id"))
    if not target_agent_id:
        raise EggError("accept", 7, "验收需要 agent_id，但 create 步骤未记录", "检查 create 步骤是否成功")

    # Create test group via WS
    group_name = f"验收测试-{state.agent_name}"
    members = _build_acceptance_members(target_agent_id, member_ids, member_types)

    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform
        runner = _gateway_runner_ref()
        if not runner:
            raise EggError("accept", 7, "Gateway 未运行", "启动 hermes gateway")
        adapter = runner.adapters.get(Platform("grix"))
        if not adapter:
            raise EggError("accept", 7, "Grix adapter 未连接", "检查 Grix 连接")

        group_result = await adapter.agent_invoke(
            action="group_create",
            params={"name": group_name, "member_ids": members["ids"], "member_types": members["types"]},
            timeout_ms=15000,
        )
    except EggError:
        raise
    except Exception as exc:
        raise EggError("accept", 7, f"创建测试群失败: {exc}", "检查 Grix WS 连接")

    if isinstance(group_result, str):
        group_result = _parse_json(group_result)
    group_data = group_result.get("data") or group_result
    session_id = _extract_session_id(group_data)
    if not session_id:
        raise EggError("accept", 7, f"测试群未返回 session_id: {group_data}", "检查群创建接口")

    # Send probe message
    probe = f"@{target_agent_id} {_clean(probe_message)}".strip()
    probe_sent_at = time.time()
    probe_msg_id = ""
    try:
        send_result = await adapter.agent_invoke(
            action="send_msg",
            params={"session_id": session_id, "message": probe},
            timeout_ms=10000,
        )
        if isinstance(send_result, str):
            send_result = _parse_json(send_result)
        probe_msg_id = _extract_msg_id(send_result.get("data") or send_result if isinstance(send_result, dict) else {})
    except Exception as exc:
        raise EggError("accept", 7, f"发送 probe 失败: {exc}", "检查消息发送接口")

    # Poll for response
    expected_lower = _clean(expected_substring).lower()
    require_expected = bool(expected_lower)
    deadline = time.time() + accept_timeout

    verified = None
    while time.time() < deadline:
        try:
            history_result = await adapter.agent_invoke(
                action="message_history",
                params={"session_id": session_id, "limit": 10},
                timeout_ms=5000,
            )
            if isinstance(history_result, str):
                history_result = _parse_json(history_result)
            messages = _extract_messages(history_result)
            for msg in messages:
                if _is_acceptance_match(msg, target_agent_id, expected_lower, require_expected, probe_sent_at, probe_msg_id):
                    verified = msg
                    break
        except Exception:
            pass
        if verified:
            break
        await _async_sleep(1.0)

    if not verified:
        raise EggError(
            "accept", 7,
            f"验收超时: agent 未在 {accept_timeout}s 内回复",
            "检查: (1) SOUL.md 内容, (2) gateway 是否在线, (3) agent 是否已连接",
        )

    mark_step_done(state, "accept", {
        "session_id": session_id,
        "verified": True,
        "target_agent_id": target_agent_id,
        "probe_message": probe,
        "reply_content": _extract_text(verified),
    })


def _build_acceptance_members(target_agent_id: str, member_ids: str, member_types: str) -> Dict[str, List[str]]:
    ids = [m.strip() for m in (member_ids or "").split(",") if m.strip()]
    types = [m.strip() for m in (member_types or "").split(",") if m.strip()]

    pairs = [(id_, types[i] if i < len(types) else "1") for i, id_ in enumerate(ids)]
    has_target = any(id_ == target_agent_id for id_, _ in pairs)
    if not has_target:
        pairs.append((target_agent_id, "2"))

    return {"ids": [p[0] for p in pairs], "types": [p[1] for p in pairs]}


def _extract_session_id(payload: Dict[str, Any]) -> str:
    for key in ["session_id", "sessionId"]:
        val = _clean(payload.get(key))
        if val:
            return val
    for nested_key in ["data", "ack", "resolvedTarget"]:
        nested = _extract_nested(payload, nested_key)
        if nested:
            sid = _extract_session_id(nested)
            if sid:
                return sid
    return ""


def _extract_messages(payload: Any) -> List[Dict[str, Any]]:
    results: List[Dict[str, Any]] = []
    _collect_messages(payload, results, 0)
    return results


def _collect_messages(value: Any, results: List[Dict[str, Any]], depth: int) -> None:
    if depth > 8 or not value:
        return
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict) and _looks_like_message(item):
                results.append(item)
            _collect_messages(item, results, depth + 1)
        return
    if isinstance(value, dict):
        for child in value.values():
            _collect_messages(child, results, depth + 1)


def _looks_like_message(record: Dict[str, Any]) -> bool:
    return bool(_extract_text(record) or _extract_msg_id(record))


def _extract_text(record: Dict[str, Any]) -> str:
    for key in ["content", "text", "message", "body", "raw_text"]:
        val = record.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for key in ["content", "message", "payload"]:
        nested = _extract_nested(record, key)
        if nested:
            t = _extract_text(nested)
            if t:
                return t
    return ""


def _extract_msg_id(record: Dict[str, Any]) -> str:
    for key in ["message_id", "messageId", "msg_id", "id"]:
        val = _clean(record.get(key))
        if val:
            return val
    return ""


def _is_acceptance_match(
    msg: Dict[str, Any],
    target_agent_id: str,
    expected_lower: str,
    require_expected: bool,
    probe_sent_at: float,
    probe_msg_id: str = "",
) -> bool:
    text = _extract_text(msg).strip()
    if not text:
        return False
    if require_expected and expected_lower not in text.lower():
        return False
    # Check sender (flat + nested)
    sender_ids = _extract_sender_ids(msg)
    if target_agent_id not in sender_ids:
        return False
    # Primary check: message ID comparison
    msg_id_str = _extract_msg_id(msg)
    if msg_id_str and probe_msg_id:
        try:
            msg_id_num = int(msg_id_str)
            probe_id_num = int(probe_msg_id)
            return msg_id_num > probe_id_num
        except (ValueError, TypeError):
            pass
    # Fallback: timestamp comparison
    msg_time = _extract_time_ms(msg)
    if msg_time is not None:
        return msg_time >= (probe_sent_at * 1000 - 1000)
    return True


def _extract_sender_ids(record: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    seen = set()
    def push(val: Any):
        text = _clean(val)
        if text and text not in seen:
            ids.append(text)
            seen.add(text)
    for key in ["sender_id", "senderId", "from_id", "fromId", "author_id", "agent_id", "agentId", "member_id", "user_id", "userId"]:
        push(record.get(key))
    for key in ["sender", "from", "author", "agent", "member", "user"]:
        nested = _extract_nested(record, key)
        if nested:
            for id_key in ["id", "agent_id", "agentId", "user_id", "userId", "member_id", "memberId"]:
                push(nested.get(id_key))
    return ids


def _extract_time_ms(record: Dict[str, Any]) -> Optional[float]:
    for key in ["created_at", "createdAt", "timestamp", "time"]:
        val = record.get(key)
        if isinstance(val, (int, float)):
            return val if val > 9999999999 else val * 1000
        text = _clean(val)
        if text and text.isdigit():
            n = int(text)
            return n if n > 9999999999 else n * 1000
    return None


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------
# Error suggestions
# ---------------------------------------------------------------------------

def suggest_for_error(step: str, error_message: str) -> str:
    lower = error_message.lower()
    suggestions = {
        "detect": "检查当前 Hermes 会话是否具备可用的 Grix 宿主能力，或提供 access_token / 账号密码",
        "install": "确认 grix-hermes 包已安装，或指定正确的 install_dir",
        "create": "检查网络连通性和认证凭证，确认 agent 创建接口正常",
        "bind": "检查 .env 文件权限和 profile 目录是否可写",
        "soul": "检查 profile 目录是否存在且有写权限",
        "gateway": "检查 Hermes CLI 是否可用，查看 gateway 日志获取详情",
        "accept": "检查: (1) SOUL.md 内容 (2) 网关是否在线 (3) agent 是否已连接到 Grix",
    }
    return suggestions.get(step, "检查相关步骤的配置和依赖")
