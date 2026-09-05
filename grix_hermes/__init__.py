"""Grix/aibot protocol platform adapter plugin for Hermes Agent."""

import inspect
from pathlib import Path

__all__ = ["register"]


PLUGIN_SKILLS = {
    "grix-access-control": {
        "description": "Approve/deny pairing codes, allow/remove senders, and set the access policy for who may message the agent.",
        "tools": ["grix_access_control"],
    },
    "grix-admin": {
        "description": "Manage Grix agents, API keys, and categories through Hermes.",
        "tools": ["grix_invoke"],
    },
    "grix-agent-dispatch": {
        "description": (
            "Dispatch one of the owner's other agents to work in a directory "
            "(callback via skill procedure report_dispatch_result / "
            "[dispatch-result] via session_send with quoted_message_id, not a "
            "grix_invoke action), and update an agent's display name and/or "
            "text introduction."
        ),
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
    "grix-owner-relay": {
        "description": (
            "Send a message as the owner into another session (dispatch "
            "callbacks use skill procedure report_dispatch_result → "
            "[dispatch-result] via session_send), or call the owner into a "
            "session for a voice talk/approval."
        ),
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
    "grix-connector-bootstrap": {
        "description": (
            "Install grix-connector on this machine and bring up its first "
            "agent: install check, Node.js check, global npm install, create "
            "the platform agent, write ~/.grix/config/agents.json, start the "
            "daemon and verify the agent is connected."
        ),
        "tools": ["grix_invoke"],
    },
    "grix-chat-state": {
        "description": "Query the chat-level task state across all the owner's chats (running / waiting / completed / failed / idle). Supports pagination and state filtering.",
        "tools": ["grix_invoke"],
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
    "tailnet-file-share": {
        "description": "Share a local file with the user via a tailnet download link.",
        "tools": ["grix_file_link"],
    },
}


# ─── 技能优先约定 ───────────────────────────────────────────────────────────
# 凡是挂在某个 plugin skill 名下的工具，注册时在其 description 末尾统一追加一句
# 指引：使用前先按对应 skill 规程执行，不要绕过 skill 直接裸调本工具。工具→技能
# 的映射直接从 PLUGIN_SKILLS 反推（单一事实源），新增技能/工具无需改动这里。
_TOOL_SKILLS: dict = {}
for _sname, _sdef in PLUGIN_SKILLS.items():
    for _tool in _sdef["tools"]:
        _skills = _TOOL_SKILLS.setdefault(_tool, [])
        if _sname not in _skills:
            _skills.append(_sname)


def _skill_guidance(tool_name: str) -> str:
    skills = _TOOL_SKILLS.get(tool_name)
    if not skills:
        return ""
    if len(skills) == 1:
        return (
            f" Before using this tool, follow the `{skills[0]}` skill's procedure "
            "first; do not invoke this tool directly without going through that "
            "skill's guidance."
        )
    return (
        " Before using this tool, follow the matching Grix skill's procedure first "
        "(the `grix-*` / `message-*` skill that covers your chosen action); do not "
        "invoke this tool directly without going through that skill's guidance."
    )


class _SkillGuidanceCtx:
    """包裹宿主 ctx：每次工具注册时，给有配套 skill 的工具描述追加「技能优先」
    指引；其余属性透传给真实 ctx。"""

    def __init__(self, ctx):
        self._ctx = ctx

    def __getattr__(self, name):
        return getattr(self._ctx, name)

    def register_tool(self, *args, **kwargs):
        tool_name = kwargs.get("name")
        guidance = _skill_guidance(tool_name) if tool_name else ""
        if guidance:
            description = kwargs.get("description")
            if isinstance(description, str) and description:
                kwargs["description"] = description + guidance
            schema = kwargs.get("schema")
            if isinstance(schema, dict) and isinstance(schema.get("description"), str):
                schema = dict(schema)
                schema["description"] = schema["description"] + guidance
                kwargs["schema"] = schema
        return self._ctx.register_tool(*args, **kwargs)


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
        allowed_users_env="GRIX_ALLOWED_USERS",
        allow_all_env="GRIX_ALLOW_ALL_USERS",
        platform_hint=(
            "You are chatting via the Grix platform. "
            "Grix supports markdown text, interactive cards, message editing, "
            "and message revocation. Keep responses concise. "
            "When you finish a task, deliver the final conclusion by calling the "
            "grix_reply tool exactly once — it quotes the message that triggered "
            "you, and that quote is the completion signal (it may hand the work "
            "to another agent). Streamed plain text is for progress notes only "
            "and never carries a quote; do not restate the conclusion as plain "
            "text after calling grix_reply."
        ),
    )

    import importlib

    guidance_ctx = _SkillGuidanceCtx(ctx)
    for _module, _fn_name in [
        ("invoke_tool", "register_invoke_tool"),
        ("reply_tool", "register_reply_tool"),
        ("auth_tools", "register_auth_tools"),
        ("egg_tool", "register_egg_tool"),
        ("update_tool", "register_update_tool"),
        ("file_link_tool", "register_file_link_tool"),
        ("file_upload_tool", "register_file_upload_tool"),
        ("access_control_tool", "register_access_control_tool"),
    ]:
        try:
            _mod = importlib.import_module(f".{_module}", __name__)
            getattr(_mod, _fn_name)(guidance_ctx)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning("Failed to register %s: %s", _module, exc)

    _register_plugin_skills(ctx)
