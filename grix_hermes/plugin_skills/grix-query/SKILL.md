---
name: grix-query
description: Query Grix contacts, sessions, and messages through the Python grix_invoke Hermes tool.
---

# Grix Query

Use the Python Hermes tool `grix_invoke`.

Supported query actions:

- `contact_search`
- `session_search`
- `message_history`
- `message_search`

Call pattern:

```text
grix_invoke(action="<ACTION>", params={...})
```

This skill is read-only. For writes, use `message-send`, `message-unsend`,
`grix-group`, or `grix-admin`.
