"""Grix/aibot protocol platform adapter plugin for Hermes Agent."""

import inspect
from pathlib import Path

__all__ = ["register"]


PLUGIN_SKILLS = {
    "grix-admin": {
        "description": "Manage Grix agents, API keys, and categories through Hermes.",
        "tools": ["grix_invoke"],
    },
    "grix-egg": {
        "description": "Install and wire a Hermes profile to Grix with grix-hermes.",
        "tools": ["grix_egg", "grix_auth", "grix_invoke", "grix_card"],
    },
    "grix-group": {
        "description": "Use Grix group operation tools through Hermes.",
        "tools": ["grix_invoke"],
    },
    "grix-query": {
        "description": "Query Grix contacts, sessions, messages, and related read-only data.",
        "tools": ["grix_invoke"],
    },
    "grix-register": {
        "description": "Register, authenticate, create API agents, and hand off Grix credentials.",
        "tools": ["grix_auth", "grix_egg"],
    },
    "grix-update": {
        "description": "Update and maintain the grix-hermes installation.",
        "tools": ["grix_update"],
    },
    "message-send": {
        "description": "Send Grix messages and cards through Hermes.",
        "tools": ["grix_invoke", "grix_card"],
    },
    "message-unsend": {
        "description": "Silently recall Grix messages through Hermes.",
        "tools": ["grix_invoke"],
    },
}


def _skill_metadata(skill_def: dict) -> dict:
    return {
        "tools": list(skill_def["tools"]),
        "tool_names": list(skill_def["tools"]),
    }


def _register_skill_with_metadata(ctx, name: str, skill_md: Path, skill_def: dict) -> None:
    register_skill = ctx.register_skill
    description = skill_def["description"]
    metadata = _skill_metadata(skill_def)
    kwargs = {}

    try:
        params = inspect.signature(register_skill).parameters
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        if accepts_kwargs or "tools" in params:
            kwargs["tools"] = metadata["tools"]
        elif "tool_names" in params:
            kwargs["tool_names"] = metadata["tool_names"]
        if accepts_kwargs or "metadata" in params:
            kwargs["metadata"] = metadata
    except (TypeError, ValueError):
        pass

    register_skill(name, skill_md, description, **kwargs)

    manager = getattr(ctx, "_manager", None)
    manifest = getattr(ctx, "manifest", None)
    plugin_name = getattr(manifest, "name", "")
    if manager is not None and plugin_name:
        plugin_skills = getattr(manager, "_plugin_skills", None)
        if isinstance(plugin_skills, dict):
            registered = plugin_skills.get(f"{plugin_name}:{name}")
            if isinstance(registered, dict):
                registered.update(metadata)


def _register_plugin_skills(ctx) -> None:
    skills_root = Path(__file__).resolve().parent / "plugin_skills"
    for skill_name, skill_def in PLUGIN_SKILLS.items():
        skill_md = skills_root / skill_name / "SKILL.md"
        if skill_md.exists():
            _register_skill_with_metadata(ctx, skill_name, skill_md, skill_def)


def register(ctx):
    from .adapter import GrixAdapter, check_grix_requirements

    ctx.register_platform(
        name="grix",
        label="Grix",
        adapter_factory=lambda cfg: GrixAdapter(cfg),
        check_fn=check_grix_requirements,
        required_env=["GRIX_ENDPOINT", "GRIX_AGENT_ID", "GRIX_API_KEY"],
        install_hint="pip install aiohttp",
        max_message_length=1800,
        emoji="🔌",
        pii_safe=False,
        allow_update_command=True,
        platform_hint=(
            "You are chatting via the Grix platform. "
            "Grix supports markdown text, interactive cards, message editing, "
            "and message revocation. Keep responses concise."
        ),
    )

    import importlib

    for _module, _fn_name in [
        ("invoke_tool", "register_invoke_tool"),
        ("auth_tools", "register_auth_tools"),
        ("egg_tool", "register_egg_tool"),
        ("update_tool", "register_update_tool"),
    ]:
        try:
            _mod = importlib.import_module(f".{_module}", __name__)
            getattr(_mod, _fn_name)(ctx)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to register %s: %s", _module, exc)

    _register_plugin_skills(ctx)
