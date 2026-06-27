"""grix_file_link tool — expose a local file over tailnet for direct download.

Logic mirrors grix-connector/src/openclaw/shared/file-serve/ (TypeScript).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from time import time
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MIME tables
# ---------------------------------------------------------------------------

_IMAGE_MIME: dict[str, str] = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".png": "image/png", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml",
    ".bmp": "image/bmp", ".tiff": "image/tiff", ".tif": "image/tiff",
    ".ico": "image/x-icon", ".avif": "image/avif",
}

_VIDEO_MIME: dict[str, str] = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".webm": "video/webm", ".ogv": "video/ogg", ".mkv": "video/x-matroska",
    ".avi": "video/x-msvideo", ".3gp": "video/3gpp", ".ts": "video/mp2t",
}

_AUDIO_MIME: dict[str, str] = {
    ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".wav": "audio/wav", ".ogg": "audio/ogg", ".oga": "audio/ogg",
    ".opus": "audio/opus", ".flac": "audio/flac", ".weba": "audio/webm",
}


def _image_mime(file_name: str) -> Optional[str]:
    return _IMAGE_MIME.get(Path(file_name).suffix.lower())


def _inline_mime(file_name: str) -> Optional[str]:
    ext = Path(file_name).suffix.lower()
    return _IMAGE_MIME.get(ext) or _VIDEO_MIME.get(ext) or _AUDIO_MIME.get(ext)


# ---------------------------------------------------------------------------
# Tailnet IP detection
# ---------------------------------------------------------------------------

def _is_tailnet_ipv4(addr: str) -> bool:
    parts = addr.split(".")
    if len(parts) != 4:
        return False
    try:
        first, second = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return first == 100 and 64 <= second <= 127


def _detect_via_cli() -> Optional[str]:
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            ip = result.stdout.strip().split("\n")[0].strip()
            if ip and _is_tailnet_ipv4(ip):
                return ip
    except Exception:
        pass
    return None


def _detect_via_interfaces() -> Optional[str]:
    # Try `ip addr` (Linux) then `ifconfig` (macOS/BSD)
    for cmd in [["ip", "addr"], ["ifconfig"]]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    m = re.search(r"inet\s+([\d.]+)", line)
                    if m and _is_tailnet_ipv4(m.group(1)):
                        return m.group(1)
        except Exception:
            continue
    return None


def detect_tailnet_ipv4() -> Optional[str]:
    ip = _detect_via_cli()
    return ip if ip is not None else _detect_via_interfaces()


# ---------------------------------------------------------------------------
# Local HTTP file server (singleton, bound to tailnet IP)
# ---------------------------------------------------------------------------

_DEFAULT_TTL_MS = 365 * 24 * 60 * 60 * 1000  # 1 年:链接只绑本机 tailnet 内网地址,实际近似不过期

_lock = threading.Lock()
_server: Optional[HTTPServer] = None
_server_host = ""
_server_port = 0
_entries: dict[str, dict] = {}  # token -> {file_path, file_name, size, expires_at}


def _sweep_expired() -> None:
    now = time() * 1000
    expired = [t for t, e in list(_entries.items()) if e["expires_at"] <= now]
    for t in expired:
        _entries.pop(t, None)


class _FileLinkHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # suppress default access log
        pass

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_GET(self) -> None:
        self._handle("GET")

    def _handle(self, method: str) -> None:
        raw_path = self.path.split("?")[0]
        m = re.match(r"^/d/([A-Za-z0-9-]+)$", raw_path)
        token = m.group(1) if m else None
        entry = _entries.get(token) if token else None
        now = time() * 1000

        if not entry or entry["expires_at"] <= now:
            self.send_response(404)
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(b"not found")
            return

        file_path: str = entry["file_path"]
        file_name: str = entry["file_name"]
        size: int = entry["size"]
        mime = _inline_mime(file_name)
        content_type = mime or "application/octet-stream"
        disposition = "inline" if mime else "attachment"
        encoded_name = urllib.parse.quote(file_name, safe="")

        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            rm = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
            if not rm or (not rm.group(1) and not rm.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if not rm.group(1):
                suffix = int(rm.group(2))
                start = 0 if suffix >= size else size - suffix
            else:
                start = int(rm.group(1))
                end = (size - 1) if not rm.group(2) else min(int(rm.group(2)), size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206

        content_length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f"{disposition}; filename*=UTF-8''{encoded_name}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(content_length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()

        if method == "HEAD":
            return

        try:
            with open(file_path, "rb") as fh:
                fh.seek(start)
                remaining = content_length
                while remaining > 0:
                    chunk = fh.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except Exception as exc:
            logger.debug("file_link send error: %s", exc)


def _ensure_server(host: str) -> int:
    global _server, _server_host, _server_port
    with _lock:
        if _server is not None and _server_host == host:
            return _server_port
        if _server is not None:
            _server.shutdown()
            _server = None
        srv = HTTPServer((host, 0), _FileLinkHandler)
        _server_host = host
        _server_port = srv.server_address[1]
        _server = srv
        t = threading.Thread(target=srv.serve_forever, daemon=True, name="grix-file-link-server")
        t.start()
        return _server_port


def _register_file(file_path: str, host: str, ttl_ms: Optional[int]) -> dict:
    stat = os.stat(file_path)
    if not os.path.isfile(file_path):
        raise ValueError(f"path is not a file: {file_path}")

    _sweep_expired()
    port = _ensure_server(host)

    token = str(uuid.uuid4())
    file_name = Path(file_path).name
    ttl = ttl_ms if (ttl_ms and ttl_ms > 0) else _DEFAULT_TTL_MS
    expires_at = int(time() * 1000) + ttl

    _entries[token] = {
        "file_path": file_path,
        "file_name": file_name,
        "size": stat.st_size,
        "expires_at": expires_at,
    }

    return {
        "url": f"http://{host}:{port}/d/{token}",
        "file_name": file_name,
        "size": stat.st_size,
        "expires_at": expires_at,
    }


# ---------------------------------------------------------------------------
# Tool schema & handler
# ---------------------------------------------------------------------------

GRIX_FILE_LINK_SCHEMA = {
    "name": "grix_file_link",
    "description": (
        "Create a direct, tailnet-only download link for a local file on this host. "
        "Use this whenever the user asks you to send, share, give, or deliver a file that exists "
        "on the machine where you run (a report, log, build artifact, export, or any local path). "
        "It returns a ready-to-use Markdown link in the `markdown` field — include that exact "
        "Markdown link in your reply so the user can click and download the file directly over "
        "the shared Tailscale network. "
        "The link is reachable only inside the tailnet, so just send it as-is — no need to worry "
        "about or mention any link lifetime. Requires this host to be on a tailnet (Tailscale running)."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "file_path": {
                "type": "string",
                "minLength": 1,
                "description": "Absolute path to a local file on this host to share with the user.",
            },
            "ttl_ms": {
                "type": "integer",
                "minimum": 10000,
                "maximum": 86400000,
                "description": "Optional link lifetime in milliseconds. Leave unset to use the long default; set only if you deliberately want a short-lived link.",
            },
        },
        "required": ["file_path"],
    },
}


async def _grix_file_link_handler(args: dict, **_kwargs) -> str:
    from tools.registry import tool_error, tool_result

    file_path = str(args.get("file_path") or "").strip()
    if not file_path:
        return tool_error("file_path is required")
    if not os.path.isabs(file_path):
        return tool_error("file_path must be an absolute path")
    if not os.path.isfile(file_path):
        return tool_error(f"file not found: {file_path}")

    ttl_ms: Optional[int] = None
    raw_ttl = args.get("ttl_ms")
    if raw_ttl is not None:
        try:
            ttl_ms = int(raw_ttl)
        except (TypeError, ValueError):
            pass

    tailnet_ip: Optional[str] = await asyncio.to_thread(detect_tailnet_ipv4)
    if not tailnet_ip:
        return tool_error("no tailnet IPv4 detected; ensure Tailscale is up on this host")

    try:
        served = await asyncio.to_thread(_register_file, file_path, tailnet_ip, ttl_ms)
    except Exception as exc:
        logger.warning("grix_file_link serve_failed: %s", exc)
        return tool_error(f"serve_failed: {exc}")

    is_image = _image_mime(served["file_name"]) is not None
    markdown = (
        f"![{served['file_name']}]({served['url']})"
        if is_image
        else f"[{served['file_name']}]({served['url']})"
    )
    return tool_result({
        "ok": True,
        "url": served["url"],
        "markdown": markdown,
        "file_name": served["file_name"],
        "is_image": is_image,
        "size": served["size"],
        "expires_at": served["expires_at"],
        "hint": "Include the `markdown` link verbatim in your reply so the user can download it over Tailscale.",
    })


def register_file_link_tool(ctx=None) -> None:
    _register = ctx.register_tool if ctx else None
    if _register:
        _register(
            name="grix_file_link",
            toolset="grix",
            schema=GRIX_FILE_LINK_SCHEMA,
            handler=_grix_file_link_handler,
            is_async=True,
            description="Create a tailnet download link for a local file.",
            emoji="📎",
        )
    else:
        from tools.registry import registry
        registry.register(
            name="grix_file_link",
            toolset="grix",
            schema=GRIX_FILE_LINK_SCHEMA,
            handler=_grix_file_link_handler,
            is_async=True,
        )
