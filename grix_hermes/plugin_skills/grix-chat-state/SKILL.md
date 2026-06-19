---
name: grix-chat-state
description: Query the chat-level task state across all the owner's chats through the Python Hermes tool grix_invoke — see which chats are running, waiting, completed, failed, or idle. Supports pagination and state filtering.
trigger: 当用户问哪些聊天任务在跑/已完成/在等待审批时
---

# Grix Chat State

Use the Python Hermes tool `grix_invoke`.

## Chat task states — `chat_state_query`

Query the task state across all the owner's chats (direct and group sessions).
All parameters are **optional**:

| param | type | default | description |
|-------|------|---------|-------------|
| `page` | int | 1 | Page number, starting from 1 |
| `page_size` | int | 10 | Items per page, max 100 |
| `state` | string | *(all)* | Filter: running / waiting_approval / waiting_question / completed / failed / idle |

```text
grix_invoke(action="chat_state_query", params={})
grix_invoke(action="chat_state_query", params={"page": 1, "page_size": 20, "state": "running"})
```

Returns one entry per session with a single mutually-exclusive state:

- `running` — working
- `waiting_approval` — blocked on the owner to approve/deny
- `waiting_question` — asked the owner a question, awaiting reply
- `completed` / `failed` — finished
- `idle` — no task / stopped

Also returns `task_title` for easy identification of each chat, along with
pagination info (`total`, `page`, `page_size`).

## Rules

1. This action is read-only — safe to call any time to orient yourself.
2. It reports per-session state, not per-message; pair it with `grix-query`
   (`message_history`) when you need the actual content.
