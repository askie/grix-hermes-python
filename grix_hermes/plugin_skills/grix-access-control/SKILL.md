---
name: grix-access-control
description: Manage sender access control with the Python Hermes tool grix_access_control — allow/remove a sender on the group-chat allowlist, or set the access policy. Approval of group visitors normally happens via the owner's one-tap approval card (no agent involvement); this tool is the auxiliary management entry.
trigger: 当用户要手动允许或移除某个发送者、或调整谁可以在群聊使用 Agent 的访问策略时
---

# Grix Access Control

Access enforcement is server-side and mostly automatic:

- **Private chats are the owner's line.** Only the owner (and users the agent is
  shared with) can DM the agent — this is a hard platform rule. The allowlist
  does NOT grant private-chat access.
- **Group visitors are approved by card.** When someone not on the allowlist
  @-mentions the agent in a group, the platform silently sends the owner a
  one-tap approval card (allow/deny). Approving adds them to the allowlist for
  group use. The agent is not involved in that flow at all.

Use the Python Hermes tool `grix_access_control` only as the auxiliary
management entry. Pick exactly one `action`:

- `allow_sender` — manually add a sender to the group-chat allowlist. Requires `sender_id`.
- `remove_sender` — remove a sender from the allowlist. Requires `sender_id`.
- `set_policy` — set the access policy. Requires `policy`, one of:
  - `allowlist` (default) — group visitors need the owner's approval; allowlisted
    senders may use the agent in group chats
  - `open` — anyone may use the agent in group chats (private chat stays owner-only)
  - `disabled` — block all senders
- `pair_approve` / `pair_deny` — approve or deny a pending access request by its
  internal `code`. Rarely needed: the owner's approval card is the normal path.

The agent owner is always exempt from the gate and can never be locked out.

Call pattern:

```text
grix_access_control(action="<ACTION>", code="...", sender_id="...", policy="...")
```

## Rules

1. Pick exactly one `action` and supply only the field it needs (`sender_id`
   for allow/remove, `policy` for set_policy, `code` for pairing actions).
2. These actions change who can reach the agent — confirm with the user before
   removing someone or switching the policy to `open`.
3. On failure, report the exact reason (e.g. expired/invalid request) instead of
   retrying with a guessed value.
