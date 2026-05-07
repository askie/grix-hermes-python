---
name: grix-update
description: Update the Python grix-hermes package through the Python grix_update Hermes tool.
---

# Grix Update

Use the Python Hermes tool `grix_update`.

Supported actions:

- `dry_run`
- `update`

Call pattern:

```text
grix_update(action="<ACTION>", params={...})
```

Run `dry_run` first unless the user explicitly asks to update immediately. The
default package target is `grix-hermes`.
