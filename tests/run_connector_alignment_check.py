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
                       "grix-owner-relay", "grix-chat-state"]
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
]
for name in CONNECTOR_COUNTERPARTS:
    text = (skills_root / name / "SKILL.md").read_text()
    check(f"{name} has trigger", bool(re.search(r"^trigger:\s*\S", text, re.M)))

# backend params are snake_case; the connector's camelCase MCP names must NOT
# leak into Hermes invoke-skill bodies (they would be rejected by the backend).
CAMEL = ["sessionId", "memberIds", "memberTypes", "memberId", "msgId", "beforeId",
         "quotedMessageId", "threadId", "agentId", "categoryId", "parentId",
         "sortOrder", "isMain", "agentName", "allMembersMuted"]
for name in ["grix-query", "grix-group", "grix-admin", "message-send", "message-unsend"]:
    text = (skills_root / name / "SKILL.md").read_text()
    leaked = [c for c in CAMEL if c in text]
    check(f"{name} has no camelCase param leakage", not leaked)

print()
if failures:
    print(f"FAILED ({len(failures)}): {failures}")
    sys.exit(1)
print("ALL CHECKS PASSED")
