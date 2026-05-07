---
name: message-unsend
description: 静默撤回 Grix 消息。通过 grix_invoke 统一接口调用 delete_msg。
version: 1.0.0
author: askie
license: MIT
metadata:
  hermes:
    tags: [grix, message, unsend, delete, recall]
    related_skills: [message-send, grix-query]
---

# Message Unsend

这个技能提供 Grix 消息静默撤回能力。

## 执行方式

使用 `grix_invoke` 统一接口，通过已有 WebSocket 连接调用：

```
grix_invoke(action="delete_msg", params={"session_id": "<SESSION_ID>", "msg_id": "<MSG_ID>"})
```

## 参数

- `session_id`：目标会话 ID
- `msg_id`：目标消息 ID

## 输出

- `{"deleted": true}`
