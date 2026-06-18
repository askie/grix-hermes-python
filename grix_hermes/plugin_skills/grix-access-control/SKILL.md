---
name: grix-access-control
description: Manage who may message this agent through the Python Hermes tool grix_access_control — approve/deny a pairing code, allow/remove a sender, or set the access policy.
trigger: 当用户要批准/拒绝配对码、允许或移除某个发送者、或调整谁可以给 Agent 发消息的访问策略时
---

# Grix Access Control

Use the Python Hermes tool `grix_access_control` to manage who may message this
agent. Pick exactly one `action`:

- `pair_approve` / `pair_deny` — approve or deny a pairing request. Requires `code`.
- `allow_sender` — add a sender to the allowlist. Requires `sender_id`.
- `remove_sender` — remove a sender. Requires `sender_id`.
- `set_policy` — set the access policy. Requires `policy`, one of:
  - `allowlist` — only allowlisted senders may message
  - `open` — anyone may message
  - `disabled` — access control off

Call pattern:

```text
grix_access_control(action="<ACTION>", code="...", sender_id="...", policy="...")
```

## Rules

1. Supply only the field the chosen action needs (`code` for pairing,
   `sender_id` for allow/remove, `policy` for set_policy).
2. These actions change who can reach the agent — confirm with the user before
   approving an unknown pairing code or switching the policy to `open`.
3. On failure, report the exact reason (e.g. expired/invalid code) instead of
   retrying with a guessed value.
