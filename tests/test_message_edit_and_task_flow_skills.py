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


def test_task_flow_discloses_missing_hermes_watchdog_primitive():
    # Hermes has no scheduled-trigger/webhook equivalent yet; the skill must
    # say so plainly rather than inventing a tool call that doesn't exist.
    assert "no self-scheduling primitive" in TASK_FLOW or "no such self-scheduling" in TASK_FLOW
