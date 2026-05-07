---
name: grix-admin
description: 程序优先的 Grix WS 管理技能。AI 只负责把自然语言整理成标准参数，再调用管理脚本执行远端 agent 与分类操作。
version: 2.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, admin, agent-management, api-key, category]
    related_skills: [grix-egg, grix-query, grix-group]
---

# Grix Admin

`grix-admin` 的唯一入口是：

```bash
node scripts/admin.js ... --json
```

这个技能只负责远端管理动作，不负责本地 profile 绑定。凡是需要把远端 agent 接到本地 Hermes，一律交给 `grix-egg`。

## 1. 程序主线

一次调用只做一个远端管理动作：

- `--action create_grix`
- `--action key_rotate`
- `--action agent_status`
- `--action list_categories`
- `--action create_category`
- `--action update_category`
- `--action assign_category`

不要让 AI 在中途手工拼 WebSocket payload；AI 只负责把用户意图转换成下面这些标准参数。

## 2. 标准入参

### 2.1 创建远端 agent

```bash
node scripts/admin.js \
  --action create_grix \
  --agent-name "<AGENT_NAME>" \
  --introduction "<INTRODUCTION>" \
  --is-main true|false \
  --category-id "<CATEGORY_ID>" \
  --category-name "<CATEGORY_NAME>"
```

规则：

- `category-id` 和 `category-name` 二选一，不要同时传
- 只传 `category-name` 时，脚本会先查分类；没有就自动创建后再分配

### 2.2 轮换 API key

```bash
node scripts/admin.js \
  --action key_rotate \
  --agent-id "<AGENT_ID>" \
  --env-file "<ENV_FILE>"
```

规则：

- `--agent-id` 必填
- `--env-file` 可选；传了就把新 key 写回 `.env`

### 2.3 状态和分类

```bash
node scripts/admin.js --action agent_status --agent-id "<AGENT_ID>"
node scripts/admin.js --action list_categories
node scripts/admin.js --action create_category --name "<NAME>" --parent-id "0" --sort-order 10
node scripts/admin.js --action update_category --category-id "<CATEGORY_ID>" --name "<NAME>"
node scripts/admin.js --action assign_category --agent-id "<AGENT_ID>" --category-id "<CATEGORY_ID>"
```

## 3. 输出与边界

- 所有成功结果都输出 JSON
- `create_grix` 返回 `createdAgent`
- `key_rotate` 返回 `rotatedAgent`
- `agent_status` 返回远端状态
- 分类动作返回分类或分配结果

重要边界：

- CLI stdout 里的 `api_key` 会被脱敏
- 如果后续马上要做 `grix-egg --route existing` 绑定，不要直接复用 CLI stdout 当成明文 key
- 需要明文 key 时，优先走：
  - `key_rotate --env-file <PATH>` 直接更新本地配置
  - 或在代码侧直接调用共享 WS client 取原始返回

## 4. AI 只参与什么

- 把自然语言整理成 `--action` 和标准参数
- 读 JSON 结果后向用户汇报创建成功、分类结果或状态结果
- 如果程序明确缺少 `agent-id`、`category-id` 之类外部信息，再回头问用户

除此之外，不要让 AI 手工接管远端管理流程。
