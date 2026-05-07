---
name: grix-register
description: Register, authenticate, and create Grix API agents through the Python grix_auth Hermes tool.
---

# Grix Register

Use the Python Hermes tool `grix_auth`.

Supported actions:

- `send_email_code`
- `register`
- `login`
- `list_agents`
- `create_agent`
- `rotate_api_key`
- `create_or_reuse_agent`

Call pattern:

```text
grix_auth(action="<ACTION>", params={...})
```

If the user wants to bind a created agent to a local Hermes profile, pass the
resulting credentials to `grix_egg` with the appropriate binding parameters.
