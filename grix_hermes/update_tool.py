"""grix-hermes update tool — upgrades via hermes plugins update."""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

PLUGIN_NAME = "grix-hermes"
PLUGIN_GIT_REPO = "askie/grix-hermes-python"

GRIX_UPDATE_SCHEMA = {
    "name": "grix_update",
    "description": (
        "Update the grix-hermes plugin via hermes CLI. "
        "Use dry_run first when the user only wants to inspect the plan."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Update action.",
                "enum": ["dry_run", "update"],
                "default": "dry_run",
            },
            "hermes_bin": {
                "type": "string",
                "description": "Path to hermes CLI binary.",
                "default": "hermes",
            },
            "profile_name": {
                "type": "string",
                "description": "Hermes profile name (optional, uses default if empty).",
            },
            "hermes_home": {
                "type": "string",
                "description": "Override HERMES_HOME directory.",
            },
        },
        "required": [],
    },
}


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
    return os.path.abspath(
        _expand_home(_clean(explicit) or os.environ.get("HERMES_HOME", "") or "~/.hermes")
    )


def _resolve_profile_root(hermes_home: str) -> str:
    current = os.path.abspath(hermes_home)
    while os.path.basename(os.path.dirname(current)) == "profiles":
        current = os.path.dirname(os.path.dirname(current))
    return current


def _build_cmd_prefix(hermes_bin: str, profile_name: str) -> List[str]:
    cmd = [hermes_bin]
    if profile_name and profile_name != "default":
        cmd += ["--profile", profile_name]
    return cmd


def _build_update_commands(params: Dict[str, Any]) -> Tuple[List[List[str]], Dict[str, str]]:
    hermes_bin = _clean(params.get("hermes_bin")) or "hermes"
    profile_name = _clean(params.get("profile_name"))
    hermes_home = _resolve_hermes_home(params.get("hermes_home"))
    profile_root = _resolve_profile_root(hermes_home)

    env = {"HERMES_HOME": profile_root}
    prefix = _build_cmd_prefix(hermes_bin, profile_name)

    commands = [
        prefix + ["plugins", "update", PLUGIN_NAME],
        prefix + ["plugins", "install", PLUGIN_GIT_REPO, "--enable"],
    ]
    return commands, env


def _run_command(
    cmd: List[str],
    *,
    env: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Tuple[int, str, str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=merged_env,
        timeout=timeout,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


async def _grix_update_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result

    params = args.get("params") if isinstance(args.get("params"), dict) else args
    action = _clean(params.get("action")) or "dry_run"
    if action not in {"dry_run", "update"}:
        return tool_error("action must be dry_run or update")

    try:
        commands, env = _build_update_commands(params)

        if action == "dry_run":
            return tool_result({
                "ok": True,
                "dry_run": True,
                "commands": commands,
                "env": env,
                "note": "Will try update first, then install from source if needed.",
            })

        # Try update first
        code, stdout, stderr = _run_command(commands[0], env=env)
        if code == 0:
            return tool_result({
                "ok": True,
                "dry_run": False,
                "method": "update",
                "cmd": commands[0],
                "code": code,
                "stdout": stdout,
                "stderr": stderr,
            })

        # Fallback: install from GitHub source
        code2, stdout2, stderr2 = _run_command(commands[1], env=env)
        return tool_result({
            "ok": code2 == 0,
            "dry_run": False,
            "method": "install_from_source",
            "cmd": commands[1],
            "code": code2,
            "stdout": stdout2,
            "stderr": stderr2,
        })
    except Exception as exc:
        return tool_error(str(exc))


def register_update_tool(ctx=None) -> None:
    _register = ctx.register_tool if ctx else None
    kwargs = dict(
        name="grix_update",
        toolset="grix",
        schema=GRIX_UPDATE_SCHEMA,
        handler=_grix_update_handler,
        check_fn=lambda: True,
        is_async=True,
        description="Update grix-hermes plugin via hermes CLI (source install).",
        emoji="UP",
    )
    if _register:
        _register(**kwargs)
    else:
        from tools.registry import registry

        registry.register(**kwargs)
