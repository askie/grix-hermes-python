---
name: grix-query
description: Query Grix contacts, sessions, and messages through the Python grix_invoke Hermes tool.
trigger: 当用户要查找联系人、搜索会话、列出可见会话、或查看某个已知会话的历史消息时
---

# Grix Query

Use the Python Hermes tool `grix_invoke`. This skill is read-only.

```text
grix_invoke(action="<ACTION>", params={...})
```

Parameter names use snake_case (sent verbatim to the backend).

- `contact_search` — params: `id` (contact ID) **or** `keyword`; optional `limit` (1–100), `offset`.
- `session_search` — params: `keyword`; optional `limit`, `offset`.
- `message_history` — params: `session_id` (required); optional `before_id` (pagination cursor), `limit`.
- `message_search` — params: `session_id` (required), `keyword`; optional `before_id`, `limit`.

For writes, use `message-send`, `message-unsend`, `grix-group`, or `grix-admin`.
