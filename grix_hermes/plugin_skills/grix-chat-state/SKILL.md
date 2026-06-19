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
| `session_id` | string | *(all)* | Query a single session by its ID; omit to return all sessions |
| `page` | int | 1 | Page number, starting from 1 |
| `page_size` | int | 10 | Items per page, max 100 |
| `state` | string | *(all)* | Filter: running / waiting_approval / waiting_question / completed / failed / idle |

```text
grix_invoke(action="chat_state_query", params={})
grix_invoke(action="chat_state_query", params={"session_id": "xxx"})
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

## Manually update a chat state — `chat_state_update`

Override the task state of a specific chat session.

| param | required | description |
|-------|----------|-------------|
| `session_id` | yes | the session to update |
| `state` | yes | running / waiting_approval / waiting_question / completed / failed / idle |
| `reason` | no | reason for the change, written to `stop_reason` |

```text
grix_invoke(action="chat_state_update", params={"session_id": "xxx", "state": "completed", "reason": "manually closed"})
```

## Rules

1. `chat_state_query` is read-only — safe to call any time to orient yourself.
2. `chat_state_update` only updates existing records; returns an error if the session has no prior state entry.
3. Both report per-session state, not per-message; pair with `grix-query` (`message_history`) when you need the actual content.
