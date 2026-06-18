---
name: grix-admin
description: Manage Grix agents, API keys, and categories through the Python grix_invoke Hermes tool.
trigger: 当用户要在 Grix 平台创建 Agent、管理分类、给 Agent 分配分类、或轮换 Agent 的 API key 时
---

# Grix Admin

Use the Python Hermes tool `grix_invoke`. This skill only handles remote Grix
agent and category management. For local Hermes profile binding, use `grix-egg`.

```text
grix_invoke(action="<ACTION>", params={...})
```

Parameter names use snake_case (sent verbatim to the backend).

- `agent_api_create` — params: `agent_name` (required); optional `introduction`,
  `is_main` (bool), `category_id`.
- `agent_api_status` — params: optional `agent_id`.
- `agent_api_key_rotate` — params: `agent_id`.
- `agent_category_list` — params: none.
- `agent_category_create` — params: `name`; optional `parent_id`, `sort_order` (int).
- `agent_category_update` — params: `category_id`; optional `name`, `parent_id`, `sort_order`.
- `agent_category_assign` — params: `agent_id`, `category_id`.
