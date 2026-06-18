---
name: grix-agent-dispatch
description: Dispatch one of the owner's agents to work in a directory, and update an agent's introduction, through the Python Hermes tool grix_invoke.
trigger: 当用户要把任务派发给 owner 名下的另一个 Agent、让某个 Agent 在指定目录干活、或修改某个 Agent 的简介时
---

# Grix Agent Dispatch

Use the Python Hermes tool `grix_invoke`.

## Dispatch a task — `dispatch_agent`

Hand work to another of the owner's agents. The backend opens (or reuses) a
private session between the owner and that agent, binds the working directory
when the agent type requires it (claude/codex/etc.), and sends the task in as
the owner so the agent starts working.

```text
grix_invoke(action="dispatch_agent", params={"agent_id": "<ID>", "cwd": "<ABS_PATH>", "task": "<TEXT>"})
```

- `agent_id` (required) — target agent's numeric ID, as a string.
- `cwd` (required) — absolute working directory for the task.
- `task` (required) — text description of what to do.

## Update an introduction — `agent_introduction_update`

```text
grix_invoke(action="agent_introduction_update", params={"agent_id": "<ID>", "introduction": "<TEXT>"})
```

- `agent_id` (required) — target agent's numeric ID, as a string.
- `introduction` (required) — new introduction text (max 300 chars).

## Rules

1. You need the exact numeric `agent_id`. Resolve it with `grix-query` first if
   you only have a name; never guess an ID.
2. `cwd` must be an absolute path the target agent can access.
3. Dispatch runs the task as the owner in a separate session — confirm the
   target agent and directory with the user when the task is consequential.
