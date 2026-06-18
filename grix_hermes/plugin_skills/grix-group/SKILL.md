---
name: grix-group
description: Manage Grix groups through the Python grix_invoke Hermes tool.
trigger: 当用户要创建、查看、退出、更新或解散群组,或群成员/禁言权限相关操作时
---

# Grix Group

Use the Python Hermes tool `grix_invoke`.

```text
grix_invoke(action="<ACTION>", params={...})
```

Parameter names use snake_case (sent verbatim to the backend).

- `group_create` — params: `name`; optional `member_ids` (array of strings),
  `member_types` (array; 1=user, 2=agent).
- `group_detail_read` — params: `session_id`.
- `group_leave_self` — params: `session_id`.
- `group_member_add` — params: `session_id`, `member_ids`, `member_types`.
- `group_member_remove` — params: `session_id`, `member_ids`.
- `group_member_role_update` — params: `session_id`, `member_id`, `role` (1=admin, 2=member).
- `group_all_members_muted_update` — params: `session_id`, `all_members_muted` (bool).
- `group_member_speaking_update` — params: `session_id`, `member_id`,
  `is_speak_muted` (bool), `can_speak_when_all_muted` (bool).
- `group_dissolve` — params: `session_id`.

Read group details before destructive changes when possible. Message sending is
handled by `message-send`; message recall is handled by `message-unsend`.
