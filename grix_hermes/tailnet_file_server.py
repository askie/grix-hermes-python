"""Tailnet HTTP file service for Hermes.

This mirrors the mobile-facing subset of grix-connector's file service:
`/ping`, `/upload`, `/download`, and `/manifest`.  The server is started during
auth when a Tailscale IPv4 can be detected, and its port is reported through
agent `host_meta` so the app can show upload/download controls.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import urllib.parse
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

UPLOAD_MAX_BYTES = 2 * 1024 * 1024 * 1024
MANIFEST_MAX_ENTRIES = 50000

_lock = threading.Lock()
_server: Optional[ThreadingHTTPServer] = None
_server_host = ""
_server_port = 0


def is_tailnet_ipv4(addr: str) -> bool:
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
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    ip = result.stdout.strip().split("\n")[0].strip()
    return ip if ip and is_tailnet_ipv4(ip) else None


def _detect_via_interfaces() -> Optional[str]:
    candidates: List[str] = []
    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        candidates.extend(info[4][0] for info in infos)
    except Exception:
        pass
    for ip in candidates:
        if is_tailnet_ipv4(ip):
            return ip
    for cmd in (["ip", "addr"], ["ifconfig"]):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", result.stdout):
            ip = match.group(1)
            if is_tailnet_ipv4(ip):
                return ip
    return None


def detect_tailnet_ipv4() -> Optional[str]:
    return _detect_via_cli() or _detect_via_interfaces()


def ensure_server_and_get_port(host: str) -> int:
    global _server, _server_host, _server_port
    with _lock:
        if _server is not None and _server_host == host:
            return _server_port
        if _server is not None:
            _server.shutdown()
            _server.server_close()
            _server = None

        srv = ThreadingHTTPServer((host, 0), _FileServiceHandler)
        _server = srv
        _server_host = host
        _server_port = int(srv.server_address[1])
        thread = threading.Thread(
            target=srv.serve_forever,
            daemon=True,
            name="grix-hermes-tailnet-file-server",
        )
        thread.start()
        return _server_port


def stop_file_server() -> None:
    global _server, _server_host, _server_port
    with _lock:
        srv = _server
        _server = None
        _server_host = ""
        _server_port = 0
    if srv is not None:
        srv.shutdown()
        srv.server_close()


def host_meta_fields() -> Dict[str, Any]:
    ip = detect_tailnet_ipv4()
    if not ip:
        return {}
    try:
        port = ensure_server_and_get_port(ip)
    except Exception:
        return {}
    if port <= 0:
        return {}
    return {"tailnet_ip": ip, "file_server_port": port}


def _request_ip(handler: BaseHTTPRequestHandler) -> str:
    raw = handler.client_address[0] if handler.client_address else ""
    return raw[7:] if raw.startswith("::ffff:") else raw


def _is_request_from_tailnet(handler: BaseHTTPRequestHandler) -> bool:
    return is_tailnet_ipv4(_request_ip(handler))


def _is_safe_absolute_path(value: str) -> bool:
    if not value or not os.path.isabs(value):
        return False
    return os.path.normpath(value) == os.path.abspath(value)


def _write_text(handler: BaseHTTPRequestHandler, status: int, body: str) -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def _write_json(handler: BaseHTTPRequestHandler, status: int, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def _parse_query(handler: BaseHTTPRequestHandler) -> Tuple[str, Dict[str, List[str]]]:
    parsed = urllib.parse.urlparse(handler.path)
    return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)


def _query_one(query: Dict[str, List[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _resolve_upload_path(directory: str, file_name: str) -> str:
    base, ext = os.path.splitext(file_name)
    candidate = os.path.join(directory, file_name)
    n = 0
    while os.path.exists(candidate):
        n += 1
        candidate = os.path.join(directory, f"{base}({n}){ext}")
    return candidate


def _read_request_body_to_file(handler: BaseHTTPRequestHandler, tmp_path: str) -> None:
    remaining_header = handler.headers.get("Content-Length")
    remaining = int(remaining_header) if remaining_header and remaining_header.isdigit() else None
    received = 0
    with open(tmp_path, "wb") as out:
        while True:
            if remaining is not None:
                if remaining <= 0:
                    break
                chunk = handler.rfile.read(min(1024 * 1024, remaining))
                remaining -= len(chunk)
            else:
                chunk = handler.rfile.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > UPLOAD_MAX_BYTES:
                raise ValueError("file too large")
            out.write(chunk)


def _handle_upload(handler: BaseHTTPRequestHandler) -> None:
    if not _is_request_from_tailnet(handler):
        _write_text(handler, HTTPStatus.FORBIDDEN, "forbidden")
        return
    _, query = _parse_query(handler)
    directory = _query_one(query, "dir")
    if not _is_safe_absolute_path(directory):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "invalid dir")
        return
    if not os.path.isdir(directory):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "dir not found")
        return

    raw_name = handler.headers.get("X-Filename", "")
    try:
        file_name = urllib.parse.unquote(raw_name)
    except Exception:
        file_name = raw_name
    if (
        not file_name
        or "/" in file_name
        or "\\" in file_name
        or file_name in {".", ".."}
    ):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "invalid filename")
        return

    dest_path = _resolve_upload_path(directory, file_name)
    tmp_path = f"{dest_path}.{uuid.uuid4()}.tmp"
    try:
        _read_request_body_to_file(handler, tmp_path)
        os.replace(tmp_path, dest_path)
    except ValueError:
        Path(tmp_path).unlink(missing_ok=True)
        _write_text(handler, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file too large")
        return
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        _write_text(handler, HTTPStatus.INTERNAL_SERVER_ERROR, "upload failed")
        return
    _write_json(
        handler,
        HTTPStatus.OK,
        {"ok": True, "path": dest_path, "name": os.path.basename(dest_path)},
    )


def _content_type(file_name: str) -> str:
    ext = Path(file_name).suffix.lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
    }.get(ext, "application/octet-stream")


def _handle_download(handler: BaseHTTPRequestHandler) -> None:
    if not _is_request_from_tailnet(handler):
        _write_text(handler, HTTPStatus.FORBIDDEN, "forbidden")
        return
    _, query = _parse_query(handler)
    file_path = _query_one(query, "path")
    if not _is_safe_absolute_path(file_path):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "invalid path")
        return
    if not os.path.exists(file_path):
        _write_text(handler, HTTPStatus.NOT_FOUND, "not found")
        return
    if not os.path.isfile(file_path):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "not a file")
        return

    size = os.path.getsize(file_path)
    file_name = os.path.basename(file_path)
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", _content_type(file_name))
    handler.send_header(
        "Content-Disposition",
        "attachment; filename*=UTF-8''" + urllib.parse.quote(file_name),
    )
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Content-Length", str(size))
    handler.end_headers()
    if handler.command != "HEAD":
        with open(file_path, "rb") as src:
            shutil.copyfileobj(src, handler.wfile, length=1024 * 1024)


def walk_manifest(root_dir: str) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = []
    unreadable = 0
    stack = [root_dir]
    while stack and len(entries) < MANIFEST_MAX_ENTRIES:
        current = stack.pop()
        try:
            names = os.listdir(current)
        except OSError:
            unreadable += 1
            continue
        for name in names:
            if len(entries) >= MANIFEST_MAX_ENTRIES:
                break
            full = os.path.join(current, name)
            if os.path.islink(full):
                continue
            rel = os.path.relpath(full, root_dir).replace(os.sep, "/")
            if os.path.isdir(full):
                entries.append({"rel": rel, "is_dir": True})
                stack.append(full)
            elif os.path.isfile(full):
                try:
                    size = os.path.getsize(full)
                except OSError:
                    unreadable += 1
                    continue
                entries.append({"rel": rel, "is_dir": False, "size": size, "abs": full})
    return {"entries": entries, "unreadable": unreadable}


def _handle_manifest(handler: BaseHTTPRequestHandler) -> None:
    if not _is_request_from_tailnet(handler):
        _write_text(handler, HTTPStatus.FORBIDDEN, "forbidden")
        return
    _, query = _parse_query(handler)
    dir_path = _query_one(query, "path")
    if not _is_safe_absolute_path(dir_path):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "invalid path")
        return
    if not os.path.exists(dir_path):
        _write_text(handler, HTTPStatus.NOT_FOUND, "not found")
        return
    if not os.path.isdir(dir_path):
        _write_text(handler, HTTPStatus.BAD_REQUEST, "not a directory")
        return
    result = walk_manifest(dir_path)
    entries = result["entries"]
    _write_json(
        handler,
        HTTPStatus.OK,
        {
            "ok": True,
            "root_name": os.path.basename(dir_path),
            "truncated": len(entries) >= MANIFEST_MAX_ENTRIES,
            "unreadable": result["unreadable"],
            "entries": entries,
        },
    )


class _FileServiceHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._dispatch()

    def do_HEAD(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def _dispatch(self) -> None:
        path, _ = _parse_query(self)
        if path == "/ping" and self.command in {"GET", "HEAD"}:
            _write_text(self, HTTPStatus.OK, "ok")
            return
        if path == "/upload" and self.command == "POST":
            _handle_upload(self)
            return
        if path == "/download" and self.command in {"GET", "HEAD"}:
            _handle_download(self)
            return
        if path == "/manifest" and self.command in {"GET", "HEAD"}:
            _handle_manifest(self)
            return
        _write_text(self, HTTPStatus.NOT_FOUND, "not found")
