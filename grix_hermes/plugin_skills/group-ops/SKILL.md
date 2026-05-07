---
name: group-ops
description: Use Grix group operation tools to create groups, inspect members, add or remove people, mute groups, and adjust speaking or role permissions.
---

Use this skill when the task is about operating a Grix group through Hermes.

Preferred tool:
- `grix_invoke`

Use these actions:
- `group_create`: create a new group
- `group_detail_read`: read a group's current settings and members
- `group_member_add`: add one or more members
- `group_member_remove`: remove one or more members
- `group_member_role_update`: update owner or admin style roles when supported by the backend
- `group_all_members_muted_update`: mute or unmute all members
- `group_member_speaking_update`: allow or block a member from speaking
- `group_leave_self`: leave a group yourself
- `group_dissolve`: dissolve a group

Working rules:
- Read group details first before destructive changes.
- Use exact IDs from prior query results instead of guessing names.
- For bulk member changes, explain who will be affected before doing it.
- After a write operation, read the group again when possible to verify the result.

Common pattern:
1. Call `grix_invoke` with `group_detail_read` or a search action to confirm the target.
2. Perform the requested group operation.
3. Verify the updated state and report the actual result.
