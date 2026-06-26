"""Presign-based media upload for the Grix/AIBot platform.

Aligns with grix-connector's uploadReplyFileToAgentMedia flow:
  1. Resolve presign URL from WebSocket endpoint
  2. POST to /oss/presign to get upload_url + media_access_url
  3. PUT file bytes to upload_url
  4. Return attachment metadata for send_msg (msg_type=2)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024  # 50 MB

UPLOADABLE_EXTENSIONS = frozenset([
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
    "txt", "md", "csv", "json", "xml",
    "zip", "rar", "7z", "tar", "gz",
    "jpg", "jpeg", "png", "webp", "gif", "bmp", "heic", "heif",
    "mp4", "mov", "m4v", "webm", "mkv", "avi",
])

_EXTENSION_MIME_MAP = {
    "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "png": "image/png", "webp": "image/webp",
    "gif": "image/gif", "bmp": "image/bmp",
    "heic": "image/heic", "heif": "image/heif",
    "mp4": "video/mp4", "mov": "video/quicktime",
    "m4v": "video/x-m4v", "webm": "video/webm",
    "mkv": "video/x-matroska", "avi": "video/x-msvideo",
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain", "md": "text/markdown",
    "csv": "text/csv", "json": "application/json", "xml": "application/xml",
    "zip": "application/zip", "rar": "application/vnd.rar",
    "7z": "application/x-7z-compressed",
    "tar": "application/x-tar", "gz": "application/gzip",
}


def resolve_content_type(file_name: str) -> str:
    ext = os.path.splitext(file_name)[1].lower().lstrip(".")
    return _EXTENSION_MIME_MAP.get(ext, "application/octet-stream")


def resolve_attachment_type(content_type: str) -> str:
    ct = content_type.lower()
    if ct.startswith("image/"):
        return "image"
    if ct.startswith("video/"):
        return "video"
    return "file"


def resolve_presign_url(ws_endpoint: str) -> str:
    parsed = urlparse(ws_endpoint)
    if parsed.scheme not in ("ws", "wss"):
        raise ValueError(f"endpoint must start with ws:// or wss://: {ws_endpoint}")

    base_path = parsed.path.rstrip("/")
    if not base_path.endswith("/ws"):
        raise ValueError(f"endpoint must end with /ws: {ws_endpoint}")
    base_path = base_path[:-3]
    if not base_path:
        raise ValueError(f"cannot derive presign path from endpoint: {ws_endpoint}")

    scheme = "https" if parsed.scheme == "wss" else "http"
    return f"{scheme}://{parsed.netloc}{base_path}/oss/presign"


def validate_file(file_path: str) -> tuple[str, str, int]:
    """Validate file and return (file_name, extension, size)."""
    normalized = file_path.strip()
    if not normalized or not os.path.isabs(normalized):
        raise ValueError("file_path must be a non-empty absolute path")
    if not os.path.isfile(normalized):
        raise FileNotFoundError(f"not a file: {normalized}")

    size = os.path.getsize(normalized)
    if size <= 0:
        raise ValueError(f"file is empty: {normalized}")
    if size > MAX_FILE_BYTES:
        raise ValueError(f"file exceeds 50MB limit ({size} bytes): {normalized}")

    file_name = os.path.basename(normalized)
    ext = os.path.splitext(file_name)[1].lower().lstrip(".")
    if not ext or ext not in UPLOADABLE_EXTENSIONS:
        raise ValueError(
            f"unsupported file type: {file_name} "
            f"(allowed: {', '.join(sorted(UPLOADABLE_EXTENSIONS))})"
        )
    return file_name, ext, size


def build_attachment_extra(
    attachment_type: str,
    file_name: str,
    access_url: str,
    content_type: str,
) -> Dict[str, Any]:
    attachment = {
        "media_url": access_url,
        "attachment_type": attachment_type,
        "file_name": file_name,
        "content_type": content_type,
    }
    return {**attachment, "attachments": [attachment]}


async def upload_file_to_media(
    *,
    ws_endpoint: str,
    api_key: str,
    session_id: str,
    file_path: str,
) -> Dict[str, Any]:
    """Upload a local file to Grix platform storage via presign.

    Returns dict with file_name, content_type, attachment_type, access_url, extra.
    """
    import aiohttp

    file_name, _, size = validate_file(file_path)
    content_type = resolve_content_type(file_name)
    attachment_type = resolve_attachment_type(content_type)

    presign_url = resolve_presign_url(ws_endpoint)
    logger.info("Requesting presign URL: %s (file=%s, %d bytes)", presign_url, file_name, size)

    presign_timeout = aiohttp.ClientTimeout(total=15)
    upload_timeout = aiohttp.ClientTimeout(total=max(60, size // (256 * 1024)))

    async with aiohttp.ClientSession() as http:
        # Step 1: request presign
        async with http.post(
            presign_url,
            json={
                "session_id": session_id.strip(),
                "filename": file_name,
                "content_type": content_type,
            },
            headers={
                "Authorization": f"Bearer {api_key.strip()}",
                "Content-Type": "application/json",
            },
            timeout=presign_timeout,
        ) as resp:
            raw_text = await resp.text()
            try:
                body = json.loads(raw_text) if raw_text else {}
            except (json.JSONDecodeError, ValueError):
                logger.debug("presign non-JSON response (%d): %.256s", resp.status, raw_text)
                raise RuntimeError(f"presign returned non-JSON response (HTTP {resp.status})")
            if not isinstance(body, dict):
                raise RuntimeError(f"presign returned unexpected response format")
            raw_code = body.get("code")
            try:
                code_ok = int(raw_code) == 0 if raw_code is not None else False
            except (TypeError, ValueError):
                code_ok = False
            if resp.status >= 400 or not code_ok:
                msg = body.get("msg") or resp.reason or "presign failed"
                raise RuntimeError(f"presign request failed: {msg}")

            data = body.get("data") or {}
            upload_url = (data.get("upload_url") or "").strip()
            access_url = (data.get("media_access_url") or "").strip()
            if not upload_url or not access_url:
                raise RuntimeError("presign returned incomplete upload_url/media_access_url")

        # Step 2: PUT file to presign URL
        file_bytes = _read_file_bytes(file_path)
        logger.info("Uploading %d bytes to presign URL", len(file_bytes))

        async with http.put(
            upload_url,
            data=file_bytes,
            headers={"Content-Type": content_type},
            timeout=upload_timeout,
        ) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"file upload failed: {resp.status} {resp.reason}")

    extra = build_attachment_extra(attachment_type, file_name, access_url, content_type)
    return {
        "file_path": file_path.strip(),
        "file_name": file_name,
        "content_type": content_type,
        "attachment_type": attachment_type,
        "access_url": access_url,
        "extra": extra,
    }


def _read_file_bytes(file_path: str) -> bytes:
    with open(file_path.strip(), "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds 50MB limit during read: {file_path}")
        return data
