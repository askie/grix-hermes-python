---
name: grix-group
description: Manage Grix groups through the Python grix_invoke Hermes tool.
---

# Grix Group

Use the Python Hermes tool `grix_invoke`.

Supported group actions:

- `group_create`
- `group_detail_read`
- `group_leave_self`
- `group_member_add`
- `group_member_remove`
- `group_member_role_update`
- `group_all_members_muted_update`
- `group_member_speaking_update`
- `group_dissolve`

Call pattern:

```text
grix_invoke(action="<ACTION>", params={...})
```

Read group details before destructive changes when possible. Message sending is
handled by `message-send`; message recall is handled by `message-unsend`.
