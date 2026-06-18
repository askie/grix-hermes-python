---
name: message-send
description: Send Grix messages and generate Grix cards through Python Hermes tools.
trigger: 当用户要主动给某个指定会话发消息、跨会话发送、或通知另一个会话时
---

# Message Send

Use the Python Hermes tools `grix_invoke` and `grix_card`.

For text messages (parameter names use snake_case, sent verbatim to the backend):

```text
grix_invoke(action="send_msg", params={"session_id": "<ID>", "content": "<TEXT>"})
```

- `session_id` (required) — target session ID.
- `content` (required) — message text (max 10000 chars).
- `msg_type` (optional) — message type, 1=text (default 1).
- `quoted_message_id` (optional) — message ID to quote/reply to.
- `thread_id` (optional) — thread ID for a threaded reply.

To reply in the current conversation, reply normally instead of using this skill.
To send AS THE OWNER into another session, use `grix-owner-relay`.

For card links:

```text
grix_card(kind="<KIND>", params={...})
```

Supported card kinds:

- `conversation`
- `user-profile`
- `egg-status`

Use `grix-query` first when the target session or user ID is unknown.
