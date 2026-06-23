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

## After updating

更新（`action="update"`）成功后，**必须自动**让新版本生效，
不要把这一步留给用户。

执行规则：

1. 仅当 `grix_update` 返回 `ok=true`（且 `dry_run=false`）时才触发重启；
   `dry_run` 或失败的更新不要重启。
2. 通过 Skill 工具调用 `/restart` 斜杠命令（`skill: "restart"`）来重启当前会话，
   代替旧的 `hermes gateway restart`。
3. 调用 `/restart` 之前给用户发一条简短回复，告知"更新成功，正在自动重启"，
   再触发重启；不要等待用户确认。
4. 若 `/restart` 不可用，回退方案才是提示用户手动执行 `/restart`。
