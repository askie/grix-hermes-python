"""Grix/aibot protocol platform adapter plugin for Hermes Agent."""

from pathlib import Path

__all__ = ["register"]


def _register_plugin_skills(ctx) -> None:
    skills_root = Path(__file__).resolve().parent / "plugin_skills"
    skill_defs = {
        "group-ops": "Use Grix group operation tools through Hermes.",
        "agent-bootstrap": "Install and wire a Hermes profile to Grix with grix-hermes.",
    }
    for skill_name, description in skill_defs.items():
        skill_md = skills_root / skill_name / "SKILL.md"
        if skill_md.exists():
            ctx.register_skill(skill_name, skill_md, description)


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
    ]:
        try:
            _mod = importlib.import_module(f".{_module}", __name__)
            getattr(_mod, _fn_name)(ctx)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to register %s: %s", _module, exc)

    _register_plugin_skills(ctx)
