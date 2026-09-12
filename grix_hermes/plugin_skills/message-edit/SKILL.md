---
name: message-edit
description: Edit the content of a message you (this agent) previously sent, in place, through the Python Hermes tool grix_invoke. Only your own plain text/markdown messages can be edited — card messages (approval/question/binding/status cards) are always rejected. Requires the owner to have granted the Edit Own Messages (message.edit) permission.
trigger: 当用户（或你自己的逻辑）想要在不发新消息的前提下，修改这个 Agent 已经发出的一条消息内容时
---

# Message Edit

Use the Python Hermes tool `grix_invoke` to rewrite a message you already
sent, in place, instead of sending a new one.

## Tool contract

```text
grix_invoke(action="message_edit", params={"session_id": "<SESSION_ID>", "msg_id": "<MSG_ID>", "content": "<NEW_CONTENT>"})
```

- `session_id` (required) — the session the message lives in.
- `msg_id` (required) — the ID of the message to edit. If unknown, find it
  first with `grix_invoke(action="message_history", ...)` /
  `grix_invoke(action="message_search", ...)` (see `grix-query`), or use the
  `msg_id` returned by the send call that created it.
- `content` (required) — the full new content; it replaces the message
  entirely, it does not append or patch.

## Rules

1. **Own messages only.** You can only edit a message you sent yourself; the
   server rejects editing another sender's message.
2. **Plain text/markdown only.** Card messages (approval cards, question
   cards, binding cards, status cards — anything rendered as a
   `grix://card/...` link) can never be edited by this tool, even if you sent
   them; the server rejects the edit. Send a new message instead if a card
   needs to change.
3. **Requires permission.** The owner must have granted this agent the
   **Edit Own Messages** (`message.edit`) scope. If the call is rejected for
   missing permission, do not retry and do not silently drop the update:
   send a **new** plain message with the content you meant to apply, and
   tell the owner to grant "编辑自己的消息 / Edit Own Messages" for this
   agent in the agent permission settings.
4. **Length limit.** `content` is capped at 10000 characters, the same limit
   as sending a message. If the content you want to write would exceed it,
   trim it (e.g. drop older history, summarize) before calling — a rejected
   over-length edit leaves the message unchanged, so check length yourself
   rather than relying on the server error as your first signal.
5. Surface any other rejection (message not found, already revoked) as-is;
   do not paper over it with a duplicate new message unless the rejection is
   specifically the permission case above.
