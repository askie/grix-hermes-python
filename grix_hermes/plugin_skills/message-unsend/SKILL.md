---
name: message-unsend
description: Recall Grix messages through the Python grix_invoke Hermes tool.
---

# Message Unsend

Use the Python Hermes tool `grix_invoke`.

Call pattern:

```text
grix_invoke(action="delete_msg", params={"session_id": "<SESSION_ID>", "msg_id": "<MSG_ID>"})
```

Use `grix-query` first when the target message ID is unknown.
