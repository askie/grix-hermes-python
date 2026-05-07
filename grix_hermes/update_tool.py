"""grix-hermes update tool registration for Hermes Agent."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List


GRIX_UPDATE_SCHEMA = {
    "name": "grix_update",
    "description": (
        "Update the Python grix-hermes package or preview the update command. "
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
            "package": {
                "type": "string",
                "description": "Package spec to upgrade.",
                "default": "grix-hermes",
            },
            "python": {
                "type": "string",
                "description": "Python executable to use. Defaults to the current interpreter.",
            },
            "extra_args": {
                "type": "array",
                "description": "Additional pip arguments, such as --index-url.",
                "items": {"type": "string"},
            },
        },
        "required": [],
    },
}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _build_command(params: Dict[str, Any]) -> List[str]:
    python_bin = _clean(params.get("python")) or sys.executable
    package = _clean(params.get("package")) or "grix-hermes"
    extra_args = params.get("extra_args") or []
    if not isinstance(extra_args, list):
        raise ValueError("extra_args must be a list of strings")
    return [
        python_bin,
        "-m",
        "pip",
        "install",
        "--upgrade",
        package,
        *[str(item) for item in extra_args],
    ]


def _run_command(cmd: List[str]) -> Dict[str, Any]:
    result = subprocess.run(
        cmd,
        cwd=str(Path.cwd()),
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "cmd": cmd,
        "code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


async def _grix_update_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result

    params = args.get("params") if isinstance(args.get("params"), dict) else args
    action = _clean(params.get("action")) or "dry_run"
    if action not in {"dry_run", "update"}:
        return tool_error("action must be dry_run or update")

    try:
        cmd = _build_command(params)
        if action == "dry_run":
            return tool_result({"ok": True, "dry_run": True, "cmd": cmd})

        result = _run_command(cmd)
        return tool_result({
            "ok": result["code"] == 0,
            "dry_run": False,
            **result,
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
        description="Update the Python grix-hermes package with pip.",
        emoji="UP",
    )
    if _register:
        _register(**kwargs)
    else:
        from tools.registry import registry

        registry.register(**kwargs)
