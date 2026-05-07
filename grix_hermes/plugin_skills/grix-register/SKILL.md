---
name: grix-register
description: 程序优先的 Grix HTTP 注册技能。AI 只负责把自然语言整理成标准子命令和参数，再调用脚本完成认证、创建和交接。
version: 2.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, register, auth, email, account]
    related_skills: [grix-egg, grix-admin]
---

# Grix Register

`grix-register` 有两个程序入口：

```bash
node scripts/grix_auth.js <subcommand> ... --json
node scripts/create_api_agent_and_bind.js ... --json
```

主入口是 `grix_auth.js`。只有在用户明确要走“HTTP 创建并立刻绑定本地 profile”时，才用 `create_api_agent_and_bind.js`。

## 1. 主线能力

`grix_auth.js` 负责 4 件事：

- `send-email-code`
- `register`
- `login`
- `create-api-agent`

`create_api_agent_and_bind.js` 负责 1 件事：

- 走 HTTP 创建远端 agent
- 然后交给 `grix-egg` 的 `bind_local` helper 完成本地绑定

## 2. 标准调用

### 2.1 发送邮箱验证码

```bash
node scripts/grix_auth.js send-email-code \
  --email "<EMAIL>" \
  --scene register \
  --base-url "https://grix.dhf.pub"
```

### 2.2 注册

```bash
node scripts/grix_auth.js register \
  --email "<EMAIL>" \
  --password "<PASSWORD>" \
  --email-code "<CODE>" \
  --base-url "https://grix.dhf.pub"
```

### 2.3 登录

```bash
node scripts/grix_auth.js login --email "<EMAIL>" --password "<PASSWORD>"
node scripts/grix_auth.js login --account "<ACCOUNT>" --password "<PASSWORD>"
```

### 2.4 创建远端 API agent

```bash
node scripts/grix_auth.js create-api-agent \
  --access-token "<TOKEN>" \
  --agent-name "<AGENT_NAME>" \
  --is-main true|false \
  --avatar-url "<AVATAR_URL>" \
  --base-url "https://grix.dhf.pub"
```

补充参数：

- `--no-reuse-existing-agent`
- `--no-rotate-key-on-reuse`

默认语义：

- 同名 `provider_type=3` agent 已存在时，脚本优先复用
- 复用时默认轮换 key
- 否则创建新 agent

### 2.5 HTTP 创建并直接绑定本地

```bash
node scripts/create_api_agent_and_bind.js \
  --access-token "<TOKEN>" \
  --agent-name "<AGENT_NAME>" \
  --profile-name "<PROFILE_NAME>" \
  --json
```

## 3. 推荐工作流

如果上层只是要“拿到可用的 access token”，主线是：

1. 必要时 `send-email-code`
2. 必要时 `register`
3. `login`

如果上层要“创建一个远端 agent”，主线是：

1. 先 `login`
2. 再 `create-api-agent`

如果上层要“创建后立刻接到本地 Hermes”，有两条路：

- 推荐：
  - `create-api-agent`
  - 把结果里的 `handoff.bind_local` 交给 `grix-egg --route existing`
- 直连 helper：
  - `create_api_agent_and_bind.js`

不要把“用户预先提供 access token”当成默认前提。正常流程里，token 应该由 `login` 或 `register` 现场产出。

## 4. 输出与边界

- `login` / `register` 成功后返回 `access_token`
- `create-api-agent` 成功后返回：
  - `agent_id`
  - `agent_name`
  - `api_endpoint`
  - `api_key`
  - `handoff.bind_local`

边界：

- 这个技能负责 HTTP 认证与远端创建
- 本地 profile 绑定的统一主线仍然是 `grix-egg`
- 除非用户明确要一次走完 HTTP 创建和本地绑定，否则不要把 `create_api_agent_and_bind.js` 当默认入口

## 5. AI 只参与什么

- 把“注册”“登录”“创建 agent”这种自然语言整理成标准子命令
- 只在程序明确缺少邮箱、密码、验证码、token 时再问用户
- 读 JSON 结果后告诉用户是拿到了 token，还是已经创建出了远端 agent
