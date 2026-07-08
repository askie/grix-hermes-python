---
name: grix-agent-dispatch
description: Dispatch one of the owner's agents to work in a directory, and update an agent's introduction, through the Python Hermes tool grix_invoke.
trigger: 当用户要把任务派发给 owner 名下的另一个 Agent、让某个 Agent 在指定目录干活、或修改某个 Agent 的简介时
---

# Grix Agent Dispatch

Use the Python Hermes tool `grix_invoke`.

## Dispatch a task — `dispatch_agent`

Hand work to another of the owner's agents. The backend creates a **new**
private session between the owner and that agent for each dispatch (it does not
reuse past sessions), binds the working directory when the agent type requires
it (claude/codex/etc.), and sends the task in as the owner so the agent starts
working.

```text
grix_invoke(action="dispatch_agent", params={"agent_id": "<ID>", "cwd": "<ABS_PATH>", "task": "<TEXT>", "title": "<SHORT_TITLE>"})
```

- `agent_id` (required) — target agent's numeric ID, as a string.
- `cwd` (required) — absolute working directory for the task.
- `task` (required) — text description of what to do.
- `title` (optional) — short title (a few words) summarizing the core of the
  task; becomes the new session's title. If omitted, the backend derives one
  from the task text.

## After dispatching — monitor and report back (don't fire-and-forget)

Dispatching is only step one. After `dispatch_agent` succeeds, **do not end your
turn there**. Stay and watch the dispatched session until it reaches a terminal
state, then report the actual result back to the user.

1. **Capture the session.** Take the target session id from the dispatch result.
   If it isn't clearly present, list sessions with `chat_state_query` (each entry
   carries its `task_title`) and match the one you just created.

2. **Poll the state.** Call `chat_state_query` scoped to that session on a calm
   cadence — wait ~15–30s between polls, never spin in a tight loop:

   ```text
   grix_invoke(action="chat_state_query", params={"session_id": "<SESSION>"})
   ```

   Each entry has one mutually-exclusive `state`; act on it:
   - `running` — still working. Keep polling.
   - `completed` — done. Go to step 3.
   - `failed` — errored. Fetch recent messages (step 3) to capture the error,
     then report the failure.
   - `waiting_approval` / `waiting_question` — the agent is blocked on the owner
     (approve/deny, or answer a question) and will **not** progress on its own.
     Fetch the latest message to see what it's asking, surface that to the user,
     and stop polling — the user has to act.
   - `idle` — no active task / stopped. If it never hit `completed`, fetch recent
     messages to see what happened, then report.

3. **Read the answer.** Once the state is terminal, pull the agent's final reply
   with `message_history`, read the latest agent message(s), and summarize the
   outcome — don't dump raw logs:

   ```text
   grix_invoke(action="message_history", params={"session_id": "<SESSION>"})
   ```

4. **Reply the result.** Send the conclusion back to the user: what the
   dispatched agent did and how it turned out.

### Long-running tasks
- If `running` persists past a few minutes (≈3 min), send a short interim
  progress note to the user so they know it's still working, then keep polling.
  Repeat at sensible intervals so the user is never left in the dark.
- A single turn can't run forever. If the task is still `running` when you've
  watched for a long stretch (≈20 min) and must wrap up, do **not** promise to
  notify the user later — you cannot send anything after the turn ends. Report
  the current state plainly and tell them how to re-check (ping you again to
  resume monitoring; the dispatched session keeps running on its own).

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
4. Dispatch is **not** fire-and-forget. After dispatching, monitor the session
   state (`chat_state_query`), read the final answer when it completes
   (`message_history`), and report the result. Never promise a future
   notification you cannot send — if you must stop while it's still running, say
   so plainly and give the user a way to re-check.
