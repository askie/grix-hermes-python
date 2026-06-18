---
name: message-unsend
description: Recall Grix messages through the Python grix_invoke Hermes tool.
trigger: 当用户要撤回、收回、删除一条已经发出的消息时
---

# Message Unsend

Use the Python Hermes tool `grix_invoke`.

Call pattern:

```text
grix_invoke(action="delete_msg", params={"session_id": "<SESSION_ID>", "msg_id": "<MSG_ID>"})
```

Use `grix-query` first when the target message ID is unknown.
