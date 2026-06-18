---
name: grix-task-status
description: Query the task state across all the owner's sessions through the Python Hermes tool grix_invoke — see which tasks are running, waiting, completed, failed, or idle.
trigger: 当用户问哪些任务在跑/已完成/在等待审批时
---

# Grix Task Status

Use the Python Hermes tool `grix_invoke`.

## Owner task states — `agent_task_query`

Query the session-level task state across all the owner's sessions. Takes no
parameters — owner and agent are resolved from the authenticated connection.

```text
grix_invoke(action="agent_task_query", params={})
```

Returns one entry per session with a single mutually-exclusive state:

- `running` — working
- `waiting_approval` — blocked on the owner to approve/deny
- `waiting_question` — asked the owner a question, awaiting reply
- `completed` / `failed` — finished
- `idle` — no task / stopped

## Rules

1. This action is read-only — safe to call any time to orient yourself.
2. It reports per-session state, not per-message; pair it with `grix-query`
   (`message_history`) when you need the actual content.
