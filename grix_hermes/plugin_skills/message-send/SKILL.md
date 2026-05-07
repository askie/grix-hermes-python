---
name: message-send
description: 在 Hermes 里发送 Grix 消息和卡片。支持当前会话回复、跨会话投递、话题回复、会话卡片、Agent 资料卡和安装状态卡。
version: 1.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, message, send, card, conversation]
    related_skills: [message-unsend, grix-query]
---

# Message Send

这个技能提供 Grix 消息投递和卡片链接生成能力。

## 能力

1. 当前会话文本回复
2. 跨会话文本发送
3. 话题消息发送
4. 指定消息回复
5. 会话卡片生成
6. Agent 资料卡生成
7. grix-egg 安装状态卡生成

## 当前会话回复

当前会话内普通回复可以直接用自然语言回复。

## 跨会话发送

使用 `grix_invoke` 统一接口，通过已有 WebSocket 连接发送：

```
grix_invoke(action="send_msg", params={"session_id": "<SESSION_ID>", "content": "<MESSAGE>"})
```

支持 `session_id:thread_id` 格式指定话题，支持 `quoted_message_id` 回复指定消息。

## 目标会话

- 已知 `session_id` 时直接发送
- 已知 `route_session_key` 时直接发送
- 需要搜索会话时使用 [grix-query](../grix-query/SKILL.md)

## 卡片链接

卡片链接使用 `grix://card/...` URL scheme 直接拼接：

会话卡片：`grix://card/conversation?session_id=<ID>&session_type=group&title=<TITLE>`
Agent 资料卡：`grix://card/user_profile?user_id=<ID>&nickname=<NAME>`
安装状态卡：`grix://card/egg_install_status?install_id=<ID>&status=running&step=installing&summary=<TEXT>`

## 卡片参数

**conversation**

- `session_id`：会话 ID
- `session_type`：`group` / `single`
- `title`：显示标题
- `peer_id`：单聊对方用户 ID

**user_profile**

- `user_id`：用户或 agent ID
- `nickname`：显示昵称
- `peer_type`：默认 `2`
- `avatar_url`：头像 URL

**egg_install_status**

- `install_id`：安装实例 ID
- `status`：`running` / `success` / `failed`
- `step`：当前步骤名
- `summary`：步骤摘要
- `target_agent_id`：目标 agent ID
- `error_code`：错误码
- `error_msg`：错误消息

## 输出

- `grix_invoke(action="send_msg")` 返回 `{"msg_id": "...", "inbox_seq": ..., "created_at": "..."}`
- 卡片链接为 `grix://card/...` 格式的 Markdown 链接

## 参考

- [Grix Card Links](../shared/references/grix-card-links.md)
