---
name: grix-owner-relay
description: Act on the owner's behalf through the Python Hermes tool grix_invoke — send a message as the owner into another session (`session_send`), or call the owner into a session for a voice talk/approval (`call_owner`). Trigger when the user asks to speak as the owner in a session, or when you need to reach the owner to discuss or get approval. Dispatch callbacks follow the grix-agent-dispatch skill procedure `report_dispatch_result` (not a grix_invoke action; it formats `[dispatch-result]` and calls `session_send`).
trigger: 当需要以 owner 身份在某会话发言、或把 owner 叫进当前会话语音沟通/审批时
---

# Grix Owner Relay

Use the Python Hermes tool `grix_invoke`.

## Speak as the owner — `session_send`

Send a message into a session **as the owner** — it shows up as if the owner
themselves sent it, **not** as you (the agent).

```text
grix_invoke(action="session_send", params={"session_id": "<ID>", "content": "<TEXT>"})
```

- `session_id` (required) — target session ID.
- `content` (required) — message text to send as the owner (max 10000 chars).

### When to use it

Only to relay on the owner's behalf into one of the owner's **other** sessions
that you are **not** a participant in. Typical case: you were dispatched to work
somewhere and need to drop a note to the owner (or to others) in a *different*
session of theirs.

**First-class use case: dispatch callback.** When you were dispatched via
`grix-agent-dispatch`, follow the skill procedure `report_dispatch_result` in
that skill (exactly 6 parameters; **not** a tool name and **not** a
`grix_invoke` action). It formats `content` as a leading `@<sender_id>` line
plus the `[dispatch-result]` wire block, and calls this action with
`session_id` = `callback_session_id`. Do not hand-roll the wire template in
the task text; if you must call `session_send` directly for a dispatch
callback, still send **only** `@<sender_id>` + the `[dispatch-result]`
block — no extra instructions, explanations, or requests outside those two
parts.

### Before you call it, make sure

1. You genuinely want to **impersonate the owner**, not speak as yourself.
2. The owner is a member of the target session (otherwise it fails on scope —
   surface the error, don't retry blindly).
3. The target session is **not** one you are conversing in / a member of.

### Never use it for

- ❌ **Sending your own reply in the conversation you are currently in.** Reply
  normally instead (or use `message-send` / `grix_invoke` action `send_msg` to
  send as yourself). Using `session_send` here makes *your* answer appear as
  the *owner's* words — i.e. the agent's text shows up as the user's message.
  This is wrong and confusing.
- ❌ **Any session you (the agent) are a member of.** The backend rejects this,
  precisely to stop the agent from impersonating the owner in its own
  conversation.
- ❌ As a generic substitute for sending a message as yourself — use
  `send_msg` for that.

To send as yourself (the agent), use the `message-send` skill
(`grix_invoke` action `send_msg`).

## Call the owner in — `call_owner`

Bring the owner into a session for a voice conversation — use this when you need
to discuss something or get an approval/review during your work. It sends the
owner an offline notification; tapping it lands them in the conversation and
auto-starts a voice-brain call.

```text
grix_invoke(action="call_owner", params={"session_id": "<ID>"})
```

- `session_id` (required) — the session to call the owner into.

## Rules

1. `session_send` only works when the owner is a member of the target
   session **and you (the agent) are not** — sending into a session you belong
   to is rejected (it would impersonate the owner in your own conversation).
   On failure, surface the error; don't retry blindly.
2. `call_owner` requires the owner to have configured a voice brain and is
   rate-limited per session. Use it only when you genuinely need the owner, not
   as a routine notification.
