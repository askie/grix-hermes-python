---
name: grix-egg
description: 程序优先的 Hermes agent 孵化技能。AI 只负责把自然语言整理成标准参数，再调用 bootstrap 程序完成整条链路。
version: 2.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, egg, bootstrap, agent-creation, profile-binding]
    related_skills: [grix-admin, grix-register, message-send]
---

# Grix Egg

`grix-egg` 的唯一执行主线是：

```bash
node scripts/bootstrap.js ... --json
```

这个技能现在按“程序为主，AI 为辅”使用：

- 程序负责完整执行：探测、安装、创建或接管、绑定、写 SOUL、启动 gateway、验收、落 checkpoint
- AI 只负责两件事：
  - 把用户自然语言整理成标准参数
  - 读取程序 JSON 结果，决定如何向用户汇报或补问缺失的外部信息

不要让 AI 在中途手动接管 create / bind / gateway / accept。除非你是在修 `grix-egg` 本身，否则不要把中间步骤拆开跑。

## 1. 程序主线

`bootstrap.js` 固定按这条顺序推进：

1. `detect`
   - `--route existing` 时直接走已有凭证绑定
   - 否则先尝试复用当前 Hermes / Grix 宿主会话
   - 宿主会话不存在时：
     - 若已提供 `--access-token`，直接走 HTTP 创建
     - 若只拿到邮箱/账号和密码，先通过 `grix-register login` 登录换取 token，再走 HTTP 创建
     - 若用户还没有账号，则先走 `send-email-code -> register -> login`，再继续创建
2. `install`
   - 安装或刷新本地 `grix-hermes` bundle
3. `create`
   - `host`：调用宿主 Grix create 能力
   - `http`：调用 HTTP create-and-bind
   - `existing`：跳过创建，直接读取已有凭证
4. `bind`
   - 把远端 agent 绑定到本地 Hermes profile
5. `soul`
   - 有 `--soul-content` 或 `--soul-file` 时写入 `SOUL.md`
6. `gateway`
   - 启动并确认 Hermes gateway 在线
7. `accept`
   - 必做
   - 程序会先创建测试群，并以该测试群 `session_id` 作为唯一验收会话
   - 程序发送验收消息时会自动在消息前拼接 `@<agent_id>` mention
   - Grix mention 格式是 `@agent_id`（不要用方括号 `@[agent_id]`）
   - 验收查询动作固定使用 `message_history`
   - 程序内部固定发送 `probe` 探针消息
   - 默认成功条件：目标 agent 在 probe 之后给出首条非空回复
   - `--expected-substring` 仅作为可选增强条件；省略时不做文本命中要求
8. 输出 JSON
   - 成功：stdout
   - 失败：stderr，并带 `step / reason / suggestion / state_file / resume_command`
9. 当前对话回传
   - 有可解析的当前会话目标时，程序默认走“简洁闭环”
   - 开始时发 1 条安装进行中状态卡
   - 创建验收群后发 1 张测试群入口卡
   - 结束时发 1 条普通文本总结，再发 1 条最终状态卡
   - 失败时发 1 条失败文本总结，再发 1 条失败状态卡
   - 回传失败不会改写 create/bind/accept 的主结果，但会在 JSON 和 checkpoint 里把 `interaction_status` 标成 `degraded`

## 2. 首次入参数

这里的“首次入参”指 AI 从用户自然语言里整理出来、第一次交给程序的标准参数。

### 2.1 新建 agent：`create_new`

默认 `--route create_new`，可以省略。

必填：

- `--agent-name`

可选业务参数：

- `--soul-content`
- `--soul-file`
- `--category-name`
- `--is-main true|false`
- `--allowed-users`
- `--allow-all-users true|false`
- `--home-channel`
- `--home-channel-name`
- `--status-target`
- `--expected-substring`
  - 仅在需要额外文本命中约束时提供；默认不需要
- `--member-ids`
- `--member-types`
- `--install-context <FILE|->`
  - 只用于补当前对话回传相关上下文，不替代已有业务参数
  - 程序会优先按 `--status-target` / `--home-channel` 回传
  - 两者都没传时，才从 install context 的 `current_chat_session_id` / `session_id` 兜底
  - 当 `--home-channel` 缺失时，程序也会用同一个 install context 自动补 profile 里的 home channel 绑定

可选环境参数：

- `--access-token`
  - 当前 Hermes / Grix 宿主会话不可复用时可直接使用
- `--email` / `--account`
- `--password`
  - 当没有 `--access-token` 时，用于先经 `grix-register login` 获取 token
  - 若用户还没有账号，则应先获取邮箱、密码、验证码，走 `send-email-code/register/login`
- `--profile-name`
  - 只有调用方想固定本地 profile 名时才需要
  - 省略时程序自动处理：
    - agent 名合法时直接复用 agent 名
    - agent 名不合法时，必须补 `--profile-name-suffix`，或直接显式传 `--profile-name`
- `--profile-name-suffix`
  - 只有 agent 名不是 ASCII-safe、但调用方又希望 profile 名可读时才需要
  - 应传 agent 的英文名或其它 ASCII-safe 标识
  - 程序会生成 `egg-<suffix>` 形式的 profile 名
- `--install-dir`
- `--hermes-home`
- `--hermes`
- `--node`

不再作为 fresh run 首次入参：

- `--install-id`
  - fresh run 省略即可
  - 程序自动生成
  - 只有 `--resume` 时才需要显式传入

### 2.2 绑定已有 agent：`existing`

必填：

- `--route existing`
- `--agent-name`
- 以下二选一：
  - `--bind-json <FILE>`
  - `--agent-id <ID> --api-endpoint <URL> --api-key <KEY>`

可选：

- 与 `create_new` 相同的 SOUL、验收、权限、runtime override 参数

### 2.3 继续上次失败任务：`resume`

必填：

- `--resume`
- `--install-id`
- `--agent-name`

可选：

- `--profile-name`
- 其他需要覆盖的首次入参

## 3. 过程参数

下面这些是程序内部推进时自己生成、自己传递、或作为结果产出的参数。AI 不应该在 fresh run 前先向用户索要它们：

- `install_id`
- `route` 的实际落点：`host` / `http` / `existing`
- create 步返回的远端凭证：
  - `agent_id`
  - `api_endpoint`
  - `api_key`
- 本地路径：
  - `profile_dir`
  - `env_path`
  - `config_path`
  - `state_file`
- 验收过程数据：
  - `session_id`
  - `probe_message_id`
  - `reply_msg_id`
  - `reply_sender_id`
- 故障恢复信息：
  - `resume_command`
  - `backup_dir`

这些都应由 `bootstrap.js` 负责生成、持有和传递，而不是让 AI 在中途接手拼装。

## 4. AI 只参与哪些环节

### 4.1 参数整理

AI 可以把自然语言转换为标准参数，例如：

- “创建一个叫雪碧的 agent，用这段人格，启动后做一次自测”
  - `--agent-name 雪碧`
  - `--soul-content ...`
  - 其余保持默认
- “agent 名是中文，但本地 profile 要带英文后缀，方便识别”
  - 在上面基础上补 `--profile-name-suffix xuebi`
- “需要额外校验回复里必须带某个词”
  - 在上面基础上补 `--expected-substring`
- “没有宿主会话，用这个 token 创建”
  - 在上面基础上补 `--access-token`
- “没有宿主会话，用户只给了邮箱/账号和密码”
  - 在上面基础上补 `--email` 或 `--account`
  - 再补 `--password`
  - 程序会先登录拿 token，再继续 HTTP fallback 创建
- “把这个已有 agent 接到本地”
  - `--route existing`
  - `--bind-json` 或显式凭证

如果当前执行上下文已经能拿到“触发这次请求的 Grix 会话”：

- 应把该 `session_id` 直接作为 `--home-channel`
- 如果能拿到会话标题，再补 `--home-channel-name`
- 同时把同一个 `session_id` 作为 `--status-target`

如果上层没有显式把这几个参数整理出来，但已经有标准 install context：

- 把 install context 通过 `--install-context <FILE|->` 交给程序
- 程序会按 `current_chat_session_id -> session_id` 的顺序自动补当前对话回传目标和 home channel
- `home_channel_name` 未显式给出时，程序会回退读 `main_agent.home_channel_name`

推荐顺序：

- 有标准 install context 时，优先只把 `--install-context` 交给程序
- 只有 install context 不可用时，才让 AI 手动补 `--home-channel` / `--status-target`

这三个值属于“从当前会话派生出来的上下文参数”，不是需要向用户额外追问的业务信息。不要因为缺这几个值就停下来问用户；拿得到就直接带上，拿不到就按默认值继续。

### 4.2 结果判读

程序跑完后，AI 可以读取 JSON 结果并判断如何对用户表达：

- `ok: true`
  - 必须在当前对话再发一条自然语言结果，不要只停在工具调用记录或终端输出
  - 优先直接使用返回里的 `summary`
  - 汇报成功
  - 告知 `agent_name`、`profile_name`
  - 如果返回里有 `acceptance.reply_content`，把这条验收回复一起告诉用户
  - 如果返回里有 `links.acceptance_conversation`，可以顺手附上测试群入口
  - 看 `interaction_status` 是否为 `full`
  - 如果是 `degraded`，说明主流程成功但当前对话回传不完整，需要把 `delivery` 里的失败点说清楚
  - 需要恢复任务时再补充 `install_id`
- `ok: false`
  - 读取 `step / reason / suggestion`
  - 同时看 `interaction_status` / `delivery`，判断当前对话回传本身是否也降级
  - 只围绕程序明确缺失的外部条件继续追问

### 4.3 语义验收

如果用户要的不是简单字符串命中，而是“回复质量是否像某种风格”这类语义判断：

- 程序仍先完成标准验收
- AI 再基于程序返回的消息历史或回复内容做补充判断

## 5. 哪些场景必须有人参与

只有下面这些场景，程序本身无法闭环，需要 AI 或用户继续参与：

- 用户还没提供必要外部信息：
  - 邮箱 / 密码
  - `access_token`
  - 已有 agent 凭证
- 当前环境没有可用宿主 create 能力，且 HTTP 条件也不满足
- 用户要做非程序默认的业务判断：
  - 是否复用现有 profile
  - 是否替换已有 agent
  - 是否接受语义上“差不多”的回复

除此之外，默认都应直接调用程序，不要把中间过程变成人工接力。

## 6. 执行上下文

`bootstrap.js` 的脚本路径取决于你的工作目录：

| 工作目录 | 命令前缀 |
|---|---|
| `grix-egg/` 子目录 | `node scripts/bootstrap.js` |
| `grix-hermes/` 仓库根目录 | `node grix-egg/scripts/bootstrap.js` |

以下模板以 `grix-egg/` 子目录为基准。如果你在仓库根目录执行，把 `scripts/bootstrap.js` 替换为 `grix-egg/scripts/bootstrap.js`。

**重要**：从源码执行时，必须保证 `--hermes-home` 指向正确的 Hermes 安装目录。如果省略，程序回退到默认 `~/.hermes`，可能导致 bind 写入一个 home 而 gateway 从另一个 home 启动，最终表现为 "No messaging platforms enabled"。建议显式传 `--hermes-home`。

## 7. 标准调用模板

### 7.1 fresh run 最小调用

```bash
node scripts/bootstrap.js \
  --agent-name "<AGENT_NAME>" \
  --hermes-home "<HERMES_HOME>" \
  --json
```

### 7.2 fresh run + SOUL

```bash
node scripts/bootstrap.js \
  --agent-name "<AGENT_NAME>" \
  --hermes-home "<HERMES_HOME>" \
  --soul-file "<SOUL_FILE>" \
  --json
```

### 7.3 fresh run + HTTP token

```bash
node scripts/bootstrap.js \
  --agent-name "<AGENT_NAME>" \
  --hermes-home "<HERMES_HOME>" \
  --access-token "<ACCESS_TOKEN>" \
  --json
```

### 7.4 existing bind

```bash
node scripts/bootstrap.js \
  --route existing \
  --agent-name "<AGENT_NAME>" \
  --hermes-home "<HERMES_HOME>" \
  --bind-json "<BIND_JSON_FILE>" \
  --json
```

### 7.5 resume

```bash
node scripts/bootstrap.js \
  --install-id "<INSTALL_ID>" \
  --agent-name "<AGENT_NAME>" \
  --hermes-home "<HERMES_HOME>" \
  --resume \
  --json
```

## 8. 常见故障排查

| 故障表现 | 最可能根因 | 排查方向 |
|---|---|---|
| accept 阶段超时，目标 agent 无回复 | mention 格式错误或 agent 未上线 | 程序已自动拼接 `@agent_id`，不要手动覆盖内部 probe；优先检查 agent 是否真的在线 |
| accept 阶段拿到了旧消息误判成功 | 查询范围或匹配条件不对 | 验收必须基于测试群 `session_id` 调 `message_history`，并只接受 probe 发出后的目标 agent 回复 |
| gateway 日志出现 `No messaging platforms enabled` | hermes-home 或 profile 不一致 | 检查 bootstrap 和 gateway 是否用了同一个 `--hermes-home`；检查 profile 内是否有 Grix 配置 |
| `MODULE_NOT_FOUND` 找不到脚本 | 工作目录不对 | 参照第 6 节调整脚本路径前缀 |
| bind 成功但 gateway 找不到 Grix 配置 | bind 写入了错误的 hermes-home | 显式传 `--hermes-home`，不要依赖默认值 |

**关键排查优先级**：看到 `No messaging platforms enabled` 时，优先检查 hermes-home / profile 是否跑偏，而不是先怀疑 agent 创建失败或网络问题。

## 9. 调试边界

下面这些脚本是 `bootstrap.js` 的子步骤，不是正常主入口：

- `scripts/bind_local.js`
- `scripts/start_gateway.js`
- `../grix-admin/scripts/admin.js`
- `../grix-register/scripts/create_api_agent_and_bind.js`

只有在修技能本身时，才单独运行它们。正常使用只走 `bootstrap.js`。
