"""Grix egg bootstrap orchestration and tool registration.

Provides the `grix_egg` tool that runs the 7-step agent incubation flow:
  detect → install → create → bind → soul → gateway → accept
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Dict, Optional

from .egg_state import (
    StateFile,
    iso_now,
    load_state,
    make_fresh_state,
    record_delivery,
    save_state,
    state_file_path,
    step_is_done,
)
from .egg_state import STEP_NAMES
from .egg_steps import (
    EggError,
    step_accept,
    step_bind,
    step_create,
    step_detect,
    step_gateway,
    step_install,
    step_soul,
    suggest_for_error,
)
from .card_links import build_egg_status_card

logger = logging.getLogger(__name__)

GRIX_EGG_SCHEMA = {
    "name": "grix_egg",
    "description": (
        "Grix agent incubation (egg) — full 7-step bootstrap flow.\n\n"
        "Steps: detect → install → create → bind → soul → gateway → accept\n\n"
        "Creates a new Grix API agent, binds it to a Hermes profile, "
        "starts the gateway, and runs acceptance testing.\n"
        "Supports checkpoint/resume via install_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "Egg action: bootstrap (full run), status (check state), dry_run (plan only).",
                "enum": ["bootstrap", "status", "dry_run"],
                "default": "bootstrap",
            },
            "install_id": {
                "type": "string",
                "description": "Unique install ID. Auto-generated if empty. Required for resume.",
            },
            "agent_name": {
                "type": "string",
                "description": "Name for the new agent. Required for create_new route.",
            },
            "route": {
                "type": "string",
                "description": "Creation route: create_new (default) or existing (bind existing credentials).",
                "enum": ["create_new", "existing"],
                "default": "create_new",
            },
            "resume": {
                "type": "boolean",
                "description": "Resume a previous bootstrap from checkpoint.",
                "default": False,
            },
            "profile_name": {
                "type": "string",
                "description": "Hermes profile name. Defaults to agent_name.",
            },
            "hermes_home": {
                "type": "string",
                "description": "Hermes home directory. Defaults to ~/.hermes.",
            },
            "install_dir": {
                "type": "string",
                "description": "Installation directory for grix-hermes package.",
            },
            "soul_content": {
                "type": "string",
                "description": "SOUL.md content for the agent.",
            },
            "soul_file": {
                "type": "string",
                "description": "Path to SOUL.md file.",
            },
            "is_main": {
                "type": "string",
                "description": "Whether agent is main (true/false). Default: true.",
                "default": "true",
            },
            "access_token": {
                "type": "string",
                "description": "Grix access token for HTTP auth path.",
            },
            "email": {
                "type": "string",
                "description": "Email for HTTP login.",
            },
            "account": {
                "type": "string",
                "description": "Account name for HTTP login.",
            },
            "password": {
                "type": "string",
                "description": "Password for HTTP login.",
            },
            "base_url": {
                "type": "string",
                "description": "Grix web base URL override.",
            },
            "agent_id": {
                "type": "string",
                "description": "Existing agent ID (for route=existing).",
            },
            "api_endpoint": {
                "type": "string",
                "description": "Existing API endpoint (for route=existing).",
            },
            "api_key": {
                "type": "string",
                "description": "Existing API key (for route=existing).",
            },
            "bind_json": {
                "type": "string",
                "description": "JSON string with bind credentials (for route=existing).",
            },
            "category_name": {
                "type": "string",
                "description": "Agent category name.",
            },
            "avatar_url": {
                "type": "string",
                "description": "Agent avatar URL.",
            },
            "account_id": {
                "type": "string",
                "description": "Grix account ID.",
            },
            "allowed_users": {
                "type": "string",
                "description": "Comma-separated allowed user IDs.",
            },
            "allow_all_users": {
                "type": "string",
                "description": "Allow all users (true/false).",
            },
            "home_channel": {
                "type": "string",
                "description": "Home channel session ID.",
            },
            "home_channel_name": {
                "type": "string",
                "description": "Home channel name.",
            },
            "delivery_target": {
                "type": "string",
                "description": "Session ID to send status updates to.",
            },
            "probe_message": {
                "type": "string",
                "description": "Probe message for acceptance test. Default: 'probe'.",
                "default": "probe",
            },
            "expected_substring": {
                "type": "string",
                "description": "Expected substring in acceptance reply.",
            },
            "accept_timeout": {
                "type": "number",
                "description": "Acceptance test timeout in seconds. Default: 15.",
                "default": 15,
            },
            "member_ids": {
                "type": "string",
                "description": "Comma-separated member IDs for test group.",
            },
            "member_types": {
                "type": "string",
                "description": "Comma-separated member types (1=user, 2=agent).",
            },
            "hermes_bin": {
                "type": "string",
                "description": "Hermes CLI binary path. Default: 'hermes'.",
                "default": "hermes",
            },
        },
        "required": [],
    },
}


async def _run_bootstrap(params: Dict[str, Any]) -> Dict[str, Any]:
    from .egg_steps import _clean, _resolve_hermes_home, _resolve_profile_dir, _resolve_profile_root, _default_install_dir

    agent_name = params.get("agent_name", "")
    install_id = params.get("install_id", "") or f"egg-{secrets.token_hex(4)}"
    route = params.get("route", "create_new")
    resume = bool(params.get("resume", False))
    hermes_home = params.get("hermes_home", "")
    delivery_target = params.get("delivery_target", "")

    resolved_home = _resolve_hermes_home(hermes_home)
    sf_path = state_file_path(resolved_home, install_id)

    # Load or create state
    if resume:
        state = load_state(sf_path)
        if not state:
            state = make_fresh_state(install_id, agent_name, route=route)
        else:
            if agent_name:
                state.agent_name = agent_name
    else:
        state = make_fresh_state(install_id, agent_name, route=route)

    # Delivery setup
    if delivery_target:
        state.delivery.target = delivery_target
        state.delivery.target_source = "status_target"

    # Backup existing files when route is "existing"
    backup_dir = ""
    if route == "existing":
        backup_dir = _backup_existing_state(resolved_home, state)

    current_step = "detect"
    try:
        # Send running status card
        _send_delivery_card(state, "running_card", "running", "preparing", "开始孵化 agent")

        # Step 1: detect
        current_step = "detect"
        step_detect(
            state,
            route=route,
            agent_id=params.get("agent_id", ""),
            api_endpoint=params.get("api_endpoint", ""),
            api_key=params.get("api_key", ""),
            bind_json=params.get("bind_json", ""),
            access_token=params.get("access_token", ""),
            email=params.get("email", ""),
            account=params.get("account", ""),
            password=params.get("password", ""),
            hermes_home=hermes_home,
            profile_name=params.get("profile_name", ""),
        )
        save_state(sf_path, state)

        # Step 2: install
        current_step = "install"
        step_install(
            state,
            hermes_home=hermes_home,
            install_dir=params.get("install_dir", ""),
        )
        save_state(sf_path, state)

        # Step 3: create
        current_step = "create"
        credentials = await step_create(
            state,
            agent_name=agent_name,
            is_main=params.get("is_main", "true") != "false",
            access_token=params.get("access_token", ""),
            email=params.get("email", ""),
            account=params.get("account", ""),
            password=params.get("password", ""),
            base_url=params.get("base_url", ""),
            category_name=params.get("category_name", ""),
            avatar_url=params.get("avatar_url", ""),
            agent_id=params.get("agent_id", ""),
            api_endpoint=params.get("api_endpoint", ""),
            api_key=params.get("api_key", ""),
            bind_json=params.get("bind_json", ""),
            hermes_home=hermes_home,
        )
        save_state(sf_path, state)

        # Step 4: bind
        current_step = "bind"
        step_bind(
            state,
            credentials=credentials,
            hermes_home=hermes_home,
            profile_name=params.get("profile_name", ""),
            agent_name=agent_name,
            is_main=params.get("is_main", "true"),
            account_id=params.get("account_id", ""),
            allowed_users=params.get("allowed_users", ""),
            allow_all_users=params.get("allow_all_users", ""),
            home_channel=params.get("home_channel", ""),
            home_channel_name=params.get("home_channel_name", ""),
            install_dir=params.get("install_dir", ""),
            hermes_bin=params.get("hermes_bin", "hermes"),
        )
        save_state(sf_path, state)

        # Step 5: soul
        current_step = "soul"
        step_soul(
            state,
            soul_content=params.get("soul_content", ""),
            soul_file=params.get("soul_file", ""),
            hermes_home=hermes_home,
        )
        save_state(sf_path, state)

        # Step 6: gateway
        current_step = "gateway"
        step_gateway(
            state,
            hermes_home=hermes_home,
            hermes_bin=params.get("hermes_bin", "hermes"),
        )
        save_state(sf_path, state)

        # Step 7: accept
        current_step = "accept"
        await step_accept(
            state,
            hermes_home=hermes_home,
            hermes_bin=params.get("hermes_bin", "hermes"),
            probe_message=params.get("probe_message", "probe"),
            expected_substring=params.get("expected_substring", ""),
            accept_timeout=float(params.get("accept_timeout", 15)),
            member_ids=params.get("member_ids", ""),
            member_types=params.get("member_types", ""),
            delivery_target=delivery_target,
        )
        save_state(sf_path, state)

        # Success
        state.completed_at = iso_now()

        accept_result = (state.steps.get("accept") or None) and state.steps["accept"].result or {}
        summary = _build_success_summary(state, accept_result)

        # Send final delivery messages
        _try_send_delivery(state, delivery_target, "final_text", summary)
        _send_delivery_card(state, "final_card", "success", "complete", "Agent 孵化完成")

        save_state(sf_path, state)

        result = {
            "ok": True,
            "install_id": install_id,
            "agent_name": state.agent_name,
            "profile_name": state.profile_name,
            "summary": summary,
            "acceptance": {
                "session_id": accept_result.get("session_id", ""),
                "verified": accept_result.get("verified", False),
                "reply_content": accept_result.get("reply_content", ""),
            },
            "steps": {name: {"status": state.steps.get(name, None) and state.steps[name].status or "pending"} for name in STEP_NAMES},
        }
        if backup_dir:
            result["backup_dir"] = backup_dir
        return result

    except EggError as exc:
        from .egg_state import mark_step_failed
        mark_step_failed(state, exc.step, str(exc))
        failure_summary = _build_failure_summary(state.agent_name or agent_name, exc.step, str(exc), exc.suggestion)
        _try_send_delivery(state, delivery_target, "failure_text", failure_summary)
        _send_delivery_card(state, "failure_card", "failed", exc.step, str(exc)[:100])
        save_state(sf_path, state)
        return {
            "ok": False,
            "step": exc.step,
            "step_number": exc.step_number,
            "reason": str(exc),
            "suggestion": exc.suggestion,
            "install_id": install_id,
            "state_file": sf_path,
            "resume_command": _build_resume_command(params, install_id),
        }
    except Exception as exc:
        from .egg_state import mark_step_failed
        mark_step_failed(state, current_step, str(exc))
        suggestion = suggest_for_error(current_step, str(exc))
        failure_summary = _build_failure_summary(state.agent_name or agent_name, current_step, str(exc), suggestion)
        _try_send_delivery(state, delivery_target, "failure_text", failure_summary)
        _send_delivery_card(state, "failure_card", "failed", current_step, str(exc)[:100])
        save_state(sf_path, state)
        return {
            "ok": False,
            "step": current_step,
            "step_number": STEP_NAMES.index(current_step) + 1 if current_step in STEP_NAMES else 0,
            "reason": str(exc),
            "suggestion": suggestion,
            "install_id": install_id,
            "state_file": sf_path,
            "resume_command": _build_resume_command(params, install_id),
        }


def _send_delivery_card(state: StateFile, kind: str, status: str, step: str, summary: str) -> None:
    target = state.delivery.target
    if not target:
        return
    try:
        card_text = build_egg_status_card(
            install_id=state.install_id,
            status=status,
            step=step,
            summary=summary,
        )
        record_delivery(state, target, state.delivery.target_source, kind, card_text, True)
    except Exception:
        pass


def _try_send_delivery(state: StateFile, target: str, kind: str, message: str) -> None:
    if not target:
        return
    record_delivery(state, target, state.delivery.target_source or "status_target", kind, message, True)


def _backup_existing_state(hermes_home: str, state: StateFile) -> str:
    import shutil
    from datetime import datetime
    profile_dir = _resolve_profile_dir(_resolve_profile_root(hermes_home), state.profile_name)
    install_dir = _default_install_dir(hermes_home)
    candidates = [
        os.path.join(profile_dir, ".env"),
        os.path.join(profile_dir, "config.yaml"),
        os.path.join(profile_dir, "SOUL.md"),
        install_dir,
    ]
    existing = [c for c in candidates if os.path.exists(c)]
    if not existing:
        return ""
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = os.path.join(hermes_home, "backups", "grix-egg", timestamp)
    os.makedirs(backup_root, exist_ok=True)
    for source in existing:
        dest = os.path.join(backup_root, os.path.basename(source))
        if os.path.isdir(source):
            shutil.copytree(source, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(source, dest)
    return backup_root


def _build_resume_command(params: Dict[str, Any], install_id: str) -> str:
    parts = ["grix_egg", "bootstrap"]
    parts.append(f"--install_id {install_id}")
    if params.get("agent_name"):
        parts.append(f"--agent_name {params['agent_name']}")
    if params.get("route"):
        parts.append(f"--route {params['route']}")
    if params.get("profile_name"):
        parts.append(f"--profile_name {params['profile_name']}")
    parts.append("--resume")
    return " ".join(parts)


def _build_failure_summary(agent_name: str, step: str, reason: str, suggestion: str) -> str:
    short_reason = reason[:120]
    short_suggestion = suggestion.split("。")[0][:80] if "。" in suggestion else suggestion[:80]
    return f"agent「{agent_name}」孵化失败，停在 {step}：{short_reason}。建议：{short_suggestion}"


def _build_success_summary(state: StateFile, accept_result: Dict[str, Any]) -> str:
    agent_name = state.agent_name
    profile = state.profile_name
    reply = (accept_result or {}).get("reply_content", "")
    head = f"agent「{agent_name}」已创建完成"
    if profile:
        head += f"，本地 profile 为「{profile}」"
    if reply:
        return f"{head}，验收回复为：{reply}"
    return f"{head}，验收已通过"


async def _grix_egg_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result

    action = (args.get("action") or "bootstrap").strip()
    params = args.get("params") if isinstance(args.get("params"), dict) else args

    if action == "status":
        install_id = (params.get("install_id") or "").strip()
        if not install_id:
            return tool_error("install_id is required for status action")
        from .egg_steps import _resolve_hermes_home
        sf_path = state_file_path(_resolve_hermes_home(params.get("hermes_home", "")), install_id)
        state = load_state(sf_path)
        if not state:
            return tool_result({"ok": False, "error": f"No state found for install_id={install_id}"})
        return tool_result({
            "ok": True,
            "install_id": state.install_id,
            "agent_name": state.agent_name,
            "profile_name": state.profile_name,
            "completed_at": state.completed_at,
            "interaction_status": state.interaction_status,
            "steps": {name: state.steps.get(name, None) and state.steps[name].status or "pending" for name in STEP_NAMES},
        })

    if action == "dry_run":
        from .egg_steps import _resolve_hermes_home
        agent_name = params.get("agent_name", "")
        install_id = params.get("install_id", "") or f"egg-{secrets.token_hex(4)}"
        route = params.get("route", "create_new")
        hermes_home = _resolve_hermes_home(params.get("hermes_home", ""))
        dry_state = make_fresh_state(install_id, agent_name, route=route)
        return tool_result({
            "ok": True,
            "dry_run": True,
            "install_id": install_id,
            "agent_name": agent_name,
            "profile_name": params.get("profile_name", "") or agent_name,
            "route": route,
            "hermes_home": hermes_home,
            "steps": {name: "pending" for name in STEP_NAMES},
        })

    try:
        result = await _run_bootstrap(params)
        return tool_result(result)
    except Exception as exc:
        logger.warning("grix_egg failed: %s", exc)
        return tool_error(f"egg bootstrap failed: {exc}")


def register_egg_tool() -> None:
    from tools.registry import registry

    registry.register(
        name="grix_egg",
        toolset="grix",
        schema=GRIX_EGG_SCHEMA,
        handler=_grix_egg_handler,
        check_fn=lambda: True,
        is_async=True,
        description="Grix agent incubation: 7-step bootstrap (detect→install→create→bind→soul→gateway→accept) with checkpoint/resume.",
        emoji="🥚",
    )
