---
name: grix-group
description: 程序优先的 Grix 群治理技能。AI 只负责把自然语言整理成标准参数，再调用群管理脚本执行。
version: 2.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, group, chat, member-management, moderation]
    related_skills: [grix-query, message-send, message-unsend]
---

# Grix Group

`grix-group` 的唯一入口是：

```bash
node scripts/group.js ... --json
```

一次调用只做一个群动作，不把建群、发消息、查历史混在一起。

## 1. 标准动作

- `--action create`
- `--action detail`
- `--action leave`
- `--action add_members`
- `--action remove_members`
- `--action update_member_role`
- `--action update_all_members_muted`
- `--action update_member_speaking`
- `--action dissolve`

## 2. 标准入参

### 2.1 建群

```bash
node scripts/group.js \
  --action create \
  --name "<GROUP_NAME>" \
  --member-ids "1001,2001" \
  --member-types "1,2"
```

规则：

- `--name` 必填
- `member-ids` 和 `member-types` 一一对应
- `member-types` 中：
  - `1` 表示用户
  - `2` 表示 agent

### 2.2 详情和生命周期

```bash
node scripts/group.js --action detail --session-id "<SESSION_ID>"
node scripts/group.js --action leave --session-id "<SESSION_ID>"
node scripts/group.js --action dissolve --session-id "<SESSION_ID>"
```

### 2.3 成员与角色

```bash
node scripts/group.js --action add_members --session-id "<SESSION_ID>" --member-ids "1002,1003"
node scripts/group.js --action remove_members --session-id "<SESSION_ID>" --member-ids "1002"
node scripts/group.js --action update_member_role --session-id "<SESSION_ID>" --member-id "1002" --member-type 1 --role 1
node scripts/group.js --action update_all_members_muted --session-id "<SESSION_ID>" --all-members-muted true --can-speak-when-all-muted false
node scripts/group.js --action update_member_speaking --session-id "<SESSION_ID>" --member-id "1002" --is-speak-muted true
```

## 3. 输出与边界

- `create` 成功后返回 `session_id`
- `detail` 返回群详情和关键配置
- 成员和禁言动作返回服务端执行结果

这个技能只管群本身：

- 发消息交给 `message-send`
- 撤回交给 `message-unsend`
- 查消息历史交给 `grix-query`

## 4. AI 只参与什么

- 把“建一个测试群、加谁、是否全员禁言”这类自然语言整理成单次群动作
- 必要时先把复杂需求拆成多次程序调用
- 读取 JSON 结果后告诉用户群是否创建成功、`session_id` 是什么、成员是否已加上

不要让 AI 手工模拟群状态，也不要把多个群动作混成一个不透明步骤。
