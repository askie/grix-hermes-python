---
name: message-unsend
description: Silently recall/unsend an already-sent message in a Grix session through the Python grix_invoke Hermes tool. After execution, end immediately without replying any confirmation text.
trigger: 当用户要撤回、收回、删除一条已经发出的消息时
---

# Message Unsend

Use the Python Hermes tool `grix_invoke` to recall a message that was already sent.

Call pattern:

```text
grix_invoke(action="delete_msg", params={"session_id": "<SESSION_ID>", "msg_id": "<MSG_ID>"})
```

## Rules

1. You need both the `session_id` and the exact `msg_id`. If the `msg_id` is
   unknown, find it first with `grix-query` (`message_history` /
   `message_search`).
2. This is a silent operation: after a successful recall, end immediately — do
   not send any confirmation message back to the chat.
3. Only recall messages that were actually sent; recalling someone else's
   message will fail on scope/permission — surface that error rather than
   retrying.
