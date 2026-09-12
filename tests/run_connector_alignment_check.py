"""Validate that the connector-aligned skills/tools are correctly wired.

Runs standalone (no Hermes host): host modules (`tools.registry`, `gateway.*`)
are stubbed so the lazy-imported handlers can execute, and a fake adapter
captures the exact agent_invoke action/params each path produces.
"""

import asyncio
import json
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# ── Stub host modules the handlers lazy-import ──────────────────────────────

calls = []  # captured (action, params) sent to the fake adapter
timeouts = []  # captured timeout_ms for each agent_invoke


class _FakeAdapter:
    class connection:
        capabilities = ["agent_invoke_v1"]

    async def agent_invoke(self, *, action, params=None, timeout_ms=None):
        calls.append((action, params))
        timeouts.append(timeout_ms)
        return {"code": 0, "echo": {"action": action, "params": params}}


class _FakeRunner:
    adapters = {"grix": _FakeAdapter()}


def _install_stubs():
    reg = types.ModuleType("tools.registry")
    reg.tool_error = lambda msg: f"ERR:{msg}"
    reg.tool_result = lambda obj: "OK:" + json.dumps(obj, default=str)

    class _Registry:
        def register(self, **kw):
            pass

    reg.registry = _Registry()
    tools_pkg = types.ModuleType("tools")
    tools_pkg.registry = reg
    sys.modules["tools"] = tools_pkg
    sys.modules["tools.registry"] = reg

    gw = types.ModuleType("gateway")
    gw_run = types.ModuleType("gateway.run")
    gw_run._gateway_runner_ref = lambda: _FakeRunner()
    gw_cfg = types.ModuleType("gateway.config")
    gw_cfg.Platform = lambda name: name  # Platform("grix") -> "grix"
    sys.modules["gateway"] = gw
    sys.modules["gateway.run"] = gw_run
    sys.modules["gateway.config"] = gw_cfg


_install_stubs()

from grix_hermes import PLUGIN_SKILLS  # noqa: E402
from grix_hermes import invoke_tool, access_control_tool  # noqa: E402

failures = []


def check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


# ── 1. grix_invoke: 5 new direct actions present in actions + schema enum ───
print("1. grix_invoke new actions")
NEW = ["dispatch_agent", "agent_introduction_update", "call_owner",
       "session_send", "chat_state_query", "chat_state_update"]
enum = invoke_tool.GRIX_INVOKE_SCHEMA["parameters"]["properties"]["action"]["enum"]
for a in NEW:
    check(f"{a} in SUPPORTED_ACTIONS", a in invoke_tool.SUPPORTED_ACTIONS)
    check(f"{a} in schema enum", a in enum)

# message_edit action: present, described with permission + card-rejection
# keywords, and forwards session_id/msg_id/content verbatim (params.py names
# match the server, not the connector's camelCase MCP tool names).
check("message_edit in SUPPORTED_ACTIONS", "message_edit" in invoke_tool.SUPPORTED_ACTIONS)
check("message_edit in schema enum", "message_edit" in enum)
_edit_desc = invoke_tool.SUPPORTED_ACTIONS.get("message_edit", "")
check("message_edit description mentions permission", "permission" in _edit_desc)
check("message_edit description mentions card rejection", "card" in _edit_desc)
check("message_edit description mentions own message only", "own" in _edit_desc.lower())

calls.clear()
timeouts.clear()
res = asyncio.run(invoke_tool._grix_invoke_handler(
    {"action": "message_edit", "params": {"session_id": "s1", "msg_id": "m1", "content": "new"}}))
check("message_edit forwarded verbatim",
      calls == [("message_edit", {"session_id": "s1", "msg_id": "m1", "content": "new"})])
check("message_edit result ok", res.startswith("OK:"))

# handler forwards action+params verbatim
calls.clear()
timeouts.clear()
res = asyncio.run(invoke_tool._grix_invoke_handler(
    {"action": "dispatch_agent", "params": {"agent_id": "7", "cwd": "/x", "task": "go"}}))
check("dispatch_agent forwarded verbatim",
      calls == [("dispatch_agent", {"agent_id": "7", "cwd": "/x", "task": "go"})])
check("dispatch_agent result ok", res.startswith("OK:"))
check("dispatch_agent default timeout 75s", timeouts == [75_000])

calls.clear()
asyncio.run(invoke_tool._grix_invoke_handler({"action": "chat_state_query", "params": {}}))
check("chat_state_query forwarded", calls == [("chat_state_query", {})])

# unknown action still rejected
bad = asyncio.run(invoke_tool._grix_invoke_handler({"action": "claude_access_control"}))
check("verbatim access-control NOT allowed via grix_invoke", bad.startswith("ERR:"))

# webhook actions: present in the action table + schema enum, and forward
# params verbatim (server param names, not the connector's MCP tool names).
print("1b. grix_invoke webhook actions")
WEBHOOK_ACTIONS = ["webhook_create", "webhook_list", "webhook_delete"]
for a in WEBHOOK_ACTIONS:
    check(f"{a} in SUPPORTED_ACTIONS", a in invoke_tool.SUPPORTED_ACTIONS)
    check(f"{a} in schema enum", a in enum)

calls.clear()
asyncio.run(invoke_tool._grix_invoke_handler(
    {"action": "webhook_create", "params": {"session_id": "s1"}}))
check("webhook_create forwarded verbatim", calls == [("webhook_create", {"session_id": "s1"})])

calls.clear()
asyncio.run(invoke_tool._grix_invoke_handler(
    {"action": "webhook_list", "params": {"session_id": "s1"}}))
check("webhook_list forwarded verbatim", calls == [("webhook_list", {"session_id": "s1"})])

calls.clear()
asyncio.run(invoke_tool._grix_invoke_handler(
    {"action": "webhook_delete", "params": {"id": "whk_1"}}))
check("webhook_delete forwarded verbatim", calls == [("webhook_delete", {"id": "whk_1"})])

# ── 2. grix_access_control: verb/payload translation ────────────────────────
print("2. grix_access_control translation")
amap = access_control_tool.ACTION_VERB_MAP
check("action map matches connector",
      amap == {"pair_approve": "pair_approve", "pair_deny": "pair_deny",
               "allow_sender": "sender_allow", "remove_sender": "sender_remove",
               "set_policy": "policy_set"})

cases = [
    ({"action": "pair_approve", "code": "ABC"}, "pair_approve", {"code": "ABC"}),
    ({"action": "allow_sender", "sender_id": "42"}, "sender_allow", {"sender_id": "42"}),
    ({"action": "remove_sender", "sender_id": "42"}, "sender_remove", {"sender_id": "42"}),
    ({"action": "set_policy", "policy": "open"}, "policy_set", {"policy": "open"}),
]
for args, verb, payload in cases:
    calls.clear()
    asyncio.run(access_control_tool._grix_access_control_handler(args))
    ok = calls == [("claude_access_control", {"verb": verb, "payload": payload})]
    check(f"{args['action']} -> claude_access_control verb={verb}", ok)

# error cases: missing required field
for args, why in [
    ({"action": "pair_approve"}, "missing code"),
    ({"action": "allow_sender"}, "missing sender_id"),
    ({"action": "set_policy", "policy": "bogus"}, "bad policy"),
    ({"action": "nope"}, "unknown action"),
]:
    out = asyncio.run(access_control_tool._grix_access_control_handler(args))
    check(f"rejects {why}", out.startswith("ERR:"))

# ── 3. skills ↔ SKILL.md alignment ──────────────────────────────────────────
print("3. skills + SKILL.md")
EXPECTED_NEW_SKILLS = ["grix-access-control", "grix-agent-dispatch",
                       "grix-owner-relay", "grix-chat-state",
                       "message-edit", "grix-task-flow",
                       "grix-scheduled-trigger"]
skills_root = ROOT / "grix_hermes" / "plugin_skills"
for s in EXPECTED_NEW_SKILLS:
    check(f"{s} in PLUGIN_SKILLS", s in PLUGIN_SKILLS)

for name, sdef in PLUGIN_SKILLS.items():
    md = skills_root / name / "SKILL.md"
    check(f"{name}/SKILL.md exists", md.exists())
    if md.exists():
        text = md.read_text()
        m = re.search(r"^name:\s*(\S+)", text, re.M)
        check(f"{name} frontmatter name matches", bool(m) and m.group(1) == name)
        # every tool the skill declares must be a real registered tool name
        for t in sdef["tools"]:
            known = t in {"grix_invoke", "grix_access_control", "grix_file_link",
                          "grix_egg", "grix_auth", "grix_update", "grix_card"}
            check(f"{name} tool {t} is a known tool", known)

# ── 4. every connector-counterpart skill carries a trigger ──────────────────
print("4. trigger fields + no camelCase param leakage")
CONNECTOR_COUNTERPARTS = [
    "grix-access-control", "grix-admin", "grix-agent-dispatch", "grix-group",
    "grix-owner-relay", "grix-query", "grix-chat-state",
    "message-send", "message-unsend", "tailnet-file-share",
    "message-edit", "grix-task-flow", "grix-scheduled-trigger",
]
for name in CONNECTOR_COUNTERPARTS:
    text = (skills_root / name / "SKILL.md").read_text()
    check(f"{name} has trigger", bool(re.search(r"^trigger:\s*\S", text, re.M)))

# backend params are snake_case; the connector's camelCase MCP names must NOT
# leak into Hermes invoke-skill bodies (they would be rejected by the backend).
CAMEL = ["sessionId", "memberIds", "memberTypes", "memberId", "msgId", "beforeId",
         "quotedMessageId", "threadId", "agentId", "categoryId", "parentId",
         "sortOrder", "isMain", "agentName", "allMembersMuted"]
for name in ["grix-query", "grix-group", "grix-admin", "message-send", "message-unsend",
             "message-edit"]:
    text = (skills_root / name / "SKILL.md").read_text()
    leaked = [c for c in CAMEL if c in text]
    check(f"{name} has no camelCase param leakage", not leaked)

# ── 5. grix-task-flow mirrors connector's required sections ────────────────
print("5. grix-task-flow required sections")
TASK_FLOW = (skills_root / "grix-task-flow" / "SKILL.md").read_text()
for phrase in ["When to split", "Recursion safety", "层级", "Watchdog"]:
    check(f"grix-task-flow mentions {phrase!r}", phrase in TASK_FLOW)
check("grix-task-flow has trigger", bool(re.search(r"^trigger:\s*\S", TASK_FLOW, re.M)))
check("grix-task-flow has no camelCase param leakage",
      not [c for c in CAMEL if c in TASK_FLOW])


# ── 6. grix-scheduled-trigger covers all four platform schedulers ──────────
print("6. grix-scheduled-trigger platform coverage")
SCHEDULED_TRIGGER_MD = skills_root / "grix-scheduled-trigger" / "SKILL.md"
check("grix-scheduled-trigger/SKILL.md exists", SCHEDULED_TRIGGER_MD.exists())
if SCHEDULED_TRIGGER_MD.exists():
    text = SCHEDULED_TRIGGER_MD.read_text()
    for keyword in ["launchd", "crontab", "systemd", "schtasks"]:
        check(f"grix-scheduled-trigger mentions {keyword!r}", keyword in text)
    for action in ["webhook_create", "webhook_list", "webhook_delete"]:
        check(f"grix-scheduled-trigger mentions {action!r}", action in text)

# ── 7. grix-task-flow no longer describes egg/watchdog as a capability gap ──
print("7. grix-task-flow has no leftover capability-gap wording")
GAP_PHRASES = [
    "no such self-scheduling primitive",
    "no dedicated skill wraps this",
    "real gap versus the connector",
    "known, disclosed limitation",
]
for phrase in GAP_PHRASES:
    check(f"grix-task-flow no longer says {phrase!r}", phrase not in TASK_FLOW)
check("grix-task-flow references grix-scheduled-trigger",
      "grix-scheduled-trigger" in TASK_FLOW)

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
