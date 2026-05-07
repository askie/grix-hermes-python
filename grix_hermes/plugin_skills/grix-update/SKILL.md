---
name: grix-update
description: 程序优先的 grix-hermes 更新技能。AI 只负责整理更新参数、读取结果和在必要时提示环境问题。
version: 2.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, update, npm, upgrade, maintenance]
    related_skills: [grix-egg]
---

# Grix Update

`grix-update` 的唯一入口是：

```bash
node scripts/grix_update.js ... --json
```

这个技能只负责把全局 npm 包更新后重新安装到目标 Hermes skills 目录，不负责手工发布 npm 包。

## 1. 程序主线

脚本固定按这 3 步执行：

1. `npm update -g @dhf-hermes/grix`
2. `npm root -g`
3. `node <global_dir>/bin/grix-hermes.js install --dest <INSTALL_DIR> --force`

## 2. 标准入参

最小调用：

```bash
node scripts/grix_update.js --json
```

常见调用：

```bash
node scripts/grix_update.js \
  --install-dir "~/.hermes/skills/grix-hermes" \
  --npm npm \
  --node node \
  --dry-run \
  --json
```

参数：

- `--install-dir`
- `--npm`
- `--node`
- `--dry-run`

## 3. 输出与边界

- `version_before`
- `version_after`
- `install_dir`
- `global_dir`

`--dry-run` 时只输出计划，不真正更新。

这个技能只处理“已发布版本的安装与更新”：

- 如果用户要发新 npm 版本，不属于这个技能的主线
- 如果用户要管理 cron，也不需要在这里手工拼复杂逻辑

## 4. Cron 口径

- `grix-hermes install` 已经会尝试创建每日更新 cron
- 只有当用户明确要单独做定时维护时，才让上层调这个技能
- 上层 cron 只需要调用这个技能，不要在 cron 里重复实现更新步骤

## 5. 环境注意事项

如果在 Hermes profile 内执行和 npm 认证相关的动作，`HOME` 可能被 profile 重定向，导致当前会话读不到真实 `~/.npmrc`。常见表现是：

- `npm whoami` 报未登录
- 发布脚本只在 auth 阶段失败

这类情况优先判断为环境问题，而不是更新脚本问题。

## 6. AI 只参与什么

- 把“更新技能包”“先 dry-run 看计划”整理成标准参数
- 读取结果后告诉用户版本有没有变化
- 只有程序失败且明显是环境问题时，再提示用户检查 npm、node 或 HOME
