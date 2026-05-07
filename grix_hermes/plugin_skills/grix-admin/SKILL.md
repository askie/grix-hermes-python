---
name: grix-admin
description: Manage Grix agents, API keys, and categories through the Python grix_invoke Hermes tool.
---

# Grix Admin

Use the Python Hermes tool `grix_invoke`.

Supported admin actions:

- `agent_api_create`
- `agent_api_status`
- `agent_api_key_rotate`
- `agent_category_list`
- `agent_category_create`
- `agent_category_update`
- `agent_category_assign`

Call pattern:

```text
grix_invoke(action="<ACTION>", params={...})
```

This skill only handles remote Grix agent and category management. For local
Hermes profile binding, use `grix-egg`.
