---
name: grix-update
description: Update the grix-hermes plugin via hermes CLI (source install from GitHub).
---

# Grix Update

Use the Hermes tool `grix_update` to update grix-hermes from source.

The tool runs `hermes plugins update grix-hermes` first; if the plugin is not
installed, it falls back to `hermes plugins install askie/grix-hermes-python --enable`.

Supported actions:

- `dry_run` — preview the commands without executing
- `update` — execute the update

Call pattern:

```text
grix_update(action="<ACTION>", params={...})
```

Run `dry_run` first unless the user explicitly asks to update immediately.
