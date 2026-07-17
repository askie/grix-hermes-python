---
name: tailnet-file-share
description: Turn a local file into a tailnet link — images render as inline previews, everything else gets a download link. Trigger when the user asks to view, send, share, download, or export any file on this machine.
trigger: 当用户要求查看、发送、分享、下载、导出本机上的任何文件时
---

When the user wants to view or obtain a file on this machine (images, logs, reports, build artifacts, exported data, etc.), you must:

1. Call the `grix_file_link` tool with the file's absolute path.
2. Copy the tool's `markdown` field into your reply verbatim — do not modify it.
   - Image files (jpg/png/gif/webp/svg, etc.): the tool returns `![filename](url)`, which renders as an inline image preview in chat.
   - Other files: the tool returns `[filename](url)`, which the user clicks to download.
3. Do not print the raw file path, and do not paste or paraphrase the file contents.
4. The link is bound to this machine's tailnet-internal address and is unreachable from the public internet. The user can reopen it any time, so under normal circumstances there is no expiry to worry about and none to mention — just send the link.

## When to use `grix_file_upload` (native attachment) instead

Besides `grix_file_link` there is a `grix_file_upload` tool: it uploads a local
file to the Grix platform, where it appears in the target session as a **native
attachment message** (images/videos display inline in chat, no tailnet needed).
It supports images, videos, documents, and archives, up to 50 MB per file.
Choose by scenario:

- The file should be a proper attachment in the chat (especially sending images or videos for the user to look at) → use `grix_file_upload`.
- The file is large (>50 MB), a clickable download link is all that's needed, or it's an arbitrary local-path artifact → use `grix_file_link`.

If the `grix_file_link` call fails (e.g. Tailscale is not connected), tell the user the local file path and let them retrieve it themselves.
