---
name: message-send
description: Send Grix messages and generate Grix cards through Python Hermes tools.
---

# Message Send

Use the Python Hermes tools `grix_invoke` and `grix_card`.

For text messages:

```text
grix_invoke(action="send_msg", params={...})
```

For card links:

```text
grix_card(kind="<KIND>", params={...})
```

Supported card kinds:

- `conversation`
- `user-profile`
- `egg-status`

Use `grix-query` first when the target session or user ID is unknown.
