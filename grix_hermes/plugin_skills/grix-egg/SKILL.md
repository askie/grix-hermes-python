---
name: grix-egg
description: "Two different things share the 'egg' name here — keep them apart. Part 1: the Grix egg market (published skill/persona packages) — search it with grix_invoke's egg_search / egg_get actions when the owner or grix-task-flow needs a ready-made capability, present matches, self-install a matched skill package, or apply a persona package to a newly created agent. Part 2: the local grix_egg Hermes tool that bootstraps/binds *this* Hermes profile to Grix (detect/install/create/bind/soul/gateway/accept)."
trigger: 当用户想要新建一个 agent/助手、问有哪些现成的蛋（技能/人格包）、需要给某个任务补一个能力缺口、或者要在这台机器上孵化/绑定 Hermes profile 到 Grix 时
---

# Grix Egg

Two jobs, in the order they happen:

1. [Discovery](#part-1--discovery-find-an-egg-in-the-market) — the owner (or
   `grix-task-flow`'s capability-gap step) wants a ready-made capability:
   find a published egg before creating a bare agent.
2. [Local bootstrap](#part-2--local-bootstrap-the-grix_egg-tool) — the
   separate `grix_egg` Python tool that incubates/binds *this* Hermes profile
   to Grix. Do not confuse the two: Part 1 is a marketplace lookup via
   `grix_invoke`; Part 2 is a local 7-step orchestration tool with its own
   name (`grix_egg`).

## Part 1 — Discovery: find an egg in the market

When the user describes an agent or capability they want ("帮我建一个跟进外贸
询盘的助手", "I need a bot that reviews PRs"), or when `grix-task-flow`'s
capability-gap step needs to fill a node, search the egg market first — a
published egg gives a tested skill set or persona in one call, via the
`grix_invoke` actions `egg_search` / `egg_get` (see `invoke_tool.py`).

### Search strategy

The backend matches keywords term-by-term (AND) against name + description +
category, so a whole sentence returns nothing. Instead:

1. Distill the description into **2–4 short keywords**: the role noun, the
   domain, the core action (e.g. `外贸`, `询盘`, `报价`; `code review`, `PR`).
2. Call `grix_invoke(action="egg_search", params={"keyword": "<term>", "locale": "<owner's locale>"})`
   **once per keyword** (`page_size` 10). Merge the results, rank by number of
   keyword hits then `install_count`.
3. If keywords return nothing, browse by category: search with no keyword and
   a likely `category_id` (categories come back in `category_id` /
   `category_name` of any result, or run a broad search first).
4. Optionally `grix_invoke(action="egg_get", params={"id": "<egg id>"})` the
   top candidates for the full description and `version_desc`.

### Present candidates

Show at most 3–5 eggs, each with: name (+ emoji), one-line description,
category, `install_count`, and what it can do for the user:

- `can_create_agent: true` — hatches into a **new agent** (persona + skills).
- `existing_agent_client_types` non-empty — installs as a **skill into an
  existing agent** of those client types.

Ask the user which one to hatch (skip the ask when `grix-task-flow` already
picked one for a node it needs filled). Do not invent capabilities that are
not in the egg description.

### Install the chosen egg

`grix_invoke(action="egg_get", ...)` returns the package URLs:
`skill_zip_url` (+ `skill_zip_sha256`) and `persona_zip_url` (+
`persona_zip_sha256`). No App round-trip is needed.

- **Skill into yourself** (`existing_agent_client_types` includes your client
  type): download `skill_zip_url`, verify the sha256 when present, unpack it,
  and copy every directory containing a `SKILL.md` into your own skill
  directory (`~/.hermes/skills/`, alongside the built-in `plugin_skills/` —
  see `scan_hermes_skills` in `exec_command.py`), overwriting a same-name
  skill. There is no `install_id` in this path — just report the installed
  skill(s) to the owner.
- **Skill into another agent** (`grix-task-flow` filling a node another agent
  should own): you cannot write into that agent's skill directory yourself —
  dispatch that agent with a task whose *first* instruction is to
  self-install the package before proceeding with the node's actual task.
- **New agent** (`can_create_agent: true`) or no existing agent could ever
  fit: create the agent with `grix_invoke(action="agent_api_create", params={"agent_name": "<NAME>", "introduction": "<TEXT>"})`
  (see `grix-admin`), then apply the egg's `persona_zip_url` to it the same
  way as the skill-package install above, then use it normally.
- No egg fits, or the user explicitly wants a blank agent → hand over to
  `grix-admin` (`agent_api_create`) and say clearly that no ready-made egg
  matched.

## Part 2 — Local bootstrap: the `grix_egg` tool

Use the Python Hermes tool `grix_egg` (distinct from the `grix_invoke`
actions above — this one has its own tool name).

Supported actions:

- `bootstrap`
- `status`
- `dry_run`

Call pattern:

```text
grix_egg(action="<ACTION>", params={...})
```

The Python tool owns the full flow: detect, install, create or reuse an agent,
bind credentials, write optional soul content, start the gateway, and run
acceptance.

Use `dry_run` before changing a profile when the user only wants to inspect the
plan. Use `status` with an `install_id` to check a previous run.
