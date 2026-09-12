"""Lock the message_edit action and the message-edit / grix-task-flow skills
against connector drift.

Mirrors grix-connector's grix_message_edit tool (src/core/mcp/tools.ts) and
its message-edit / grix-task-flow default skills, adapted for Hermes's
grix_invoke action names and snake_case params.
"""

import re
from pathlib import Path

from grix_hermes import PLUGIN_SKILLS
from grix_hermes import invoke_tool

ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "grix_hermes" / "plugin_skills"
MESSAGE_EDIT = (SKILLS_ROOT / "message-edit" / "SKILL.md").read_text(encoding="utf-8")
TASK_FLOW = (SKILLS_ROOT / "grix-task-flow" / "SKILL.md").read_text(encoding="utf-8")
SCHEDULED_TRIGGER = (SKILLS_ROOT / "grix-scheduled-trigger" / "SKILL.md").read_text(encoding="utf-8")
GRIX_EGG = (SKILLS_ROOT / "grix-egg" / "SKILL.md").read_text(encoding="utf-8")


def test_message_edit_action_registered_with_permission_and_card_limits():
    assert "message_edit" in invoke_tool.SUPPORTED_ACTIONS
    enum = invoke_tool.GRIX_INVOKE_SCHEMA["parameters"]["properties"]["action"]["enum"]
    assert "message_edit" in enum
    assert "message_edit" in invoke_tool.GRIX_INVOKE_SCHEMA["description"]

    desc = invoke_tool.SUPPORTED_ACTIONS["message_edit"]
    assert "own" in desc.lower()
    assert "card" in desc.lower()
    assert "permission" in desc.lower()
    assert "session_id" in desc and "msg_id" in desc and "content" in desc


def test_message_edit_skill_frontmatter_and_contract():
    m = re.search(r"^name:\s*(\S+)", MESSAGE_EDIT, re.M)
    assert m and m.group(1) == "message-edit"
    assert re.search(r"^trigger:\s*\S", MESSAGE_EDIT, re.M)

    assert 'grix_invoke(action="message_edit"' in MESSAGE_EDIT
    assert '"session_id"' in MESSAGE_EDIT
    assert '"msg_id"' in MESSAGE_EDIT
    assert '"content"' in MESSAGE_EDIT

    # backend param names are snake_case; the connector's camelCase MCP
    # names (sessionId/msgId) must not leak into the Hermes skill body.
    assert "sessionId" not in MESSAGE_EDIT
    assert "msgId" not in MESSAGE_EDIT

    # core rules carried over from the connector skill
    assert "Own messages only" in MESSAGE_EDIT
    assert "card" in MESSAGE_EDIT.lower()
    assert "message.edit" in MESSAGE_EDIT
    assert "10000" in MESSAGE_EDIT


def test_plugin_skills_registers_message_edit_and_task_flow():
    assert "message-edit" in PLUGIN_SKILLS
    assert "grix-task-flow" in PLUGIN_SKILLS
    assert PLUGIN_SKILLS["message-edit"]["tools"] == ["grix_invoke"]
    assert PLUGIN_SKILLS["grix-task-flow"]["tools"] == ["grix_invoke"]


def test_task_flow_skill_frontmatter_and_required_sections():
    m = re.search(r"^name:\s*(\S+)", TASK_FLOW, re.M)
    assert m and m.group(1) == "grix-task-flow"
    assert re.search(r"^trigger:\s*\S", TASK_FLOW, re.M)

    for phrase in ["When to split", "Recursion safety", "层级", "Watchdog"]:
        assert phrase in TASK_FLOW

    # depth cap and no-relay-back rules must survive the port
    assert "N ≥ 3" in TASK_FLOW
    assert "report_dispatch_result" in TASK_FLOW
    assert "dispatch_agent" in TASK_FLOW
    assert 'grix_invoke(action="message_edit"' in TASK_FLOW

    # no camelCase leakage from the connector's MCP tool params
    for camel in ("sessionId", "msgId", "agentId"):
        assert camel not in TASK_FLOW


def test_task_flow_arms_watchdog_via_scheduled_trigger():
    # Hermes now has a scheduled-trigger/webhook equivalent (grix-scheduled-
    # trigger); the skill must call into it rather than disclosing a gap.
    assert "grix-scheduled-trigger" in TASK_FLOW
    assert "no self-scheduling primitive" not in TASK_FLOW
    assert "no such self-scheduling" not in TASK_FLOW


def test_webhook_actions_registered():
    for action in ("webhook_create", "webhook_list", "webhook_delete"):
        assert action in invoke_tool.SUPPORTED_ACTIONS
        enum = invoke_tool.GRIX_INVOKE_SCHEMA["parameters"]["properties"]["action"]["enum"]
        assert action in enum
    assert "webhook_create" in invoke_tool.GRIX_INVOKE_SCHEMA["description"]


def test_scheduled_trigger_skill_frontmatter_and_platform_coverage():
    m = re.search(r"^name:\s*(\S+)", SCHEDULED_TRIGGER, re.M)
    assert m and m.group(1) == "grix-scheduled-trigger"
    assert re.search(r"^trigger:\s*\S", SCHEDULED_TRIGGER, re.M)

    for action in ("webhook_create", "webhook_list", "webhook_delete"):
        assert f'action="{action}"' in SCHEDULED_TRIGGER

    # all four platform schedulers must be documented
    for keyword in ("launchd", "crontab", "systemd", "schtasks"):
        assert keyword in SCHEDULED_TRIGGER

    assert "grix-scheduled-trigger" in PLUGIN_SKILLS
    assert PLUGIN_SKILLS["grix-scheduled-trigger"]["tools"] == ["grix_invoke"]


def test_grix_egg_skill_covers_marketplace_discovery():
    # Aligns with the connector's grix-egg default skill (marketplace search)
    # while keeping the pre-existing local bootstrap tool documentation.
    assert "egg_search" in GRIX_EGG
    assert "egg_get" in GRIX_EGG
    assert "can_create_agent" in GRIX_EGG
    assert "existing_agent_client_types" in GRIX_EGG
    # local bootstrap documentation must survive the port unchanged
    assert "grix_egg(action=" in GRIX_EGG
    assert "dry_run" in GRIX_EGG
