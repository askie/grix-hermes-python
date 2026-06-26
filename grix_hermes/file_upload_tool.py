"""Grix file upload tool — upload local files to platform storage and send as media messages."""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

GRIX_FILE_UPLOAD_SCHEMA = {
    "name": "grix_file_upload",
    "description": (
        "Upload a local file to the Grix platform and send it as a media message.\n\n"
        "Supports images (jpg/png/webp/gif/bmp/heic/heif), "
        "videos (mp4/mov/m4v/webm/mkv/avi), "
        "documents (pdf/doc/docx/xls/xlsx/ppt/pptx/txt/md/csv/json/xml), "
        "and archives (zip/rar/7z/tar/gz). Max 50 MB.\n\n"
        "Use this instead of grix_file_link when you want the file to appear "
        "as a native attachment in the chat (visible inline for images/videos), "
        "rather than a tailnet download link."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to a local file to upload.",
            },
            "session_id": {
                "type": "string",
                "description": "Target session ID to send the file to.",
            },
            "caption": {
                "type": "string",
                "description": "Optional text caption for the media message.",
                "default": "",
            },
            "reply_to_message_id": {
                "type": "string",
                "description": "Optional message ID to quote/reply to.",
            },
        },
        "required": ["file_path", "session_id"],
    },
}


def _check_file_upload() -> bool:
    try:
        from gateway.run import _gateway_runner_ref
        from gateway.config import Platform

        runner = _gateway_runner_ref()
        if not runner:
            return False
        adapter = runner.adapters.get(Platform("grix"))
        if not adapter:
            return False
        return bool(adapter.connection.endpoint and adapter.connection.api_key)
    except Exception:
        return False


async def _grix_file_upload_handler(args: dict, **kwargs) -> str:
    from tools.registry import tool_error, tool_result
    from gateway.config import Platform

    file_path = (args.get("file_path") or "").strip()
    session_id = (args.get("session_id") or "").strip()
    caption = (args.get("caption") or "").strip()
    reply_to = (args.get("reply_to_message_id") or "").strip() or None

    if not file_path:
        return tool_error("file_path is required")
    if not session_id:
        return tool_error("session_id is required")

    try:
        from .media_upload import validate_file, upload_file_to_media
        validate_file(file_path)
    except (ValueError, FileNotFoundError) as exc:
        return tool_error(str(exc))

    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
        if not runner:
            return tool_error("Gateway is not running")

        adapter = runner.adapters.get(Platform("grix"))
        if not adapter:
            return tool_error("Grix adapter is not connected")

        ws_endpoint = adapter.connection.endpoint
        api_key = adapter.connection.api_key
        if not ws_endpoint or not api_key:
            return tool_error("Grix connection config incomplete (missing endpoint or api_key)")

        upload_result = await upload_file_to_media(
            ws_endpoint=ws_endpoint,
            api_key=api_key,
            session_id=session_id,
            file_path=file_path,
        )

        client = await adapter._get_ready_client(operation="file_upload")
        if not client:
            return tool_error("Grix transport is not connected")

        send_result = await client.send_media(
            session_id=session_id,
            content=caption or f"[{upload_result['attachment_type']}]",
            extra=upload_result["extra"],
            reply_to_message_id=reply_to,
        )

        return tool_result({
            "ok": True,
            "file_name": upload_result["file_name"],
            "attachment_type": upload_result["attachment_type"],
            "access_url": upload_result["access_url"],
            "message_id": send_result.get("message_id"),
        })
    except Exception as exc:
        logger.warning("grix_file_upload failed: %s", exc)
        return tool_error(f"file upload failed: {exc}")


def register_file_upload_tool(ctx=None) -> None:
    _register = ctx.register_tool if ctx else None
    if _register:
        _register(
            name="grix_file_upload",
            toolset="grix",
            schema=GRIX_FILE_UPLOAD_SCHEMA,
            handler=_grix_file_upload_handler,
            check_fn=_check_file_upload,
            is_async=True,
            description="Upload a local file to Grix platform and send as media message.",
            emoji="📤",
        )
    else:
        from tools.registry import registry
        registry.register(
            name="grix_file_upload",
            toolset="grix",
            schema=GRIX_FILE_UPLOAD_SCHEMA,
            handler=_grix_file_upload_handler,
            check_fn=_check_file_upload,
            is_async=True,
            description="Upload a local file to Grix platform and send as media message.",
            emoji="📤",
        )
