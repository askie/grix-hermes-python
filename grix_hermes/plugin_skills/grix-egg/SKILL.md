---
name: grix-egg
description: Bootstrap or bind a Grix-backed Hermes profile through the Python grix_egg Hermes tool.
---

# Grix Egg

Use the Python Hermes tool `grix_egg`.

Supported actions:

- `bootstrap`
- `status`
- `dry_run`

Call pattern:

```text
grix_egg(action="<ACTION>", params={...})
```

The Python tool owns the full flow: detect, install, create or reuse an agent,
bind credentials, write optional soul content, start the gateway, and run
acceptance.

Use `dry_run` before changing a profile when the user only wants to inspect the
plan. Use `status` with an `install_id` to check a previous run.
