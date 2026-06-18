---
name: grix-owner-relay
description: Act on the owner's behalf through the Python Hermes tool grix_invoke — send a message as the owner into another session, or call the owner into a session for a voice talk/approval.
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

Use it **only** to relay on the owner's behalf into one of the owner's **other**
sessions that you are **not** a participant in (e.g. you were dispatched to work
somewhere and need to drop a note to the owner in a different session).

Never use it for:

- ❌ Sending your own reply in the conversation you are currently in — reply
  normally, or use `message-send` (`grix_invoke` action `send_msg`) to send as
  yourself.
- ❌ Any session you (the agent) are a member of — the backend rejects this.

## Call the owner in — `call_owner`

Bring the owner into a session for a voice conversation — use when you need to
discuss something or get an approval/review during your work. It sends the owner
an offline notification; tapping it lands them in the conversation and
auto-starts a voice-brain call.

```text
grix_invoke(action="call_owner", params={"session_id": "<ID>"})
```

- `session_id` (required) — the session to call the owner into.

## Rules

1. `session_send` only works when the owner is a member of the target session
   **and you are not**. On failure, surface the error; don't retry blindly.
2. `call_owner` requires the owner to have configured a voice brain and is
   rate-limited per session. Use it only when you genuinely need the owner.
