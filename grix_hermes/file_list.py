"""Grix file-list local action handler.

行为与 grix-connector（src/core/files）保持一致：自由导航、跨平台路径归一化、
导航哨兵 ::root/::home、扩展名过滤、固定 MIME 映射，以及统一的错误码语义。
machine_name 不在此处注入，由调用方（adapter）在边界处统一加上。
"""

from __future__ import annotations

import os
import platform
import re
import stat as stat_mod
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# 扩展名 → MIME 的固定映射，对齐 grix-connector，不依赖系统 mimetypes 以保证两端一致。
_EXT_MIME_MAP: Dict[str, str] = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "txt": "text/plain",
    "md": "text/markdown",
    "csv": "text/csv",
    "json": "application/json",
    "xml": "application/xml",
    "yaml": "text/yaml",
    "yml": "text/yaml",
    "html": "text/html",
    "css": "text/css",
    "js": "application/javascript",
    "ts": "application/typescript",
    "zip": "application/zip",
    "rar": "application/x-rar-compressed",
    "7z": "application/x-7z-compressed",
    "tar": "application/x-tar",
    "gz": "application/gzip",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "avi": "video/x-msvideo",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "aac": "audio/aac",
}


def handle_file_list_action(
    params: Dict[str, Any],
    *,
    fallback_dir: Optional[str] = None,
) -> Dict[str, Any]:
    parent_id = params.get("parent_id") or None
    show_hidden = bool(params.get("show_hidden"))
    raw_ext = params.get("allowed_extensions")
    allowed = _normalize_ext_filter(raw_ext if isinstance(raw_ext, list) else None)

    is_windows = platform.system() == "Windows"

    # Windows 根视图：无 parent_id 时列出盘符。
    if is_windows and not parent_id:
        return _ok(_list_windows_drives(), "")

    # 跨平台导航哨兵：前端只发 token，由 agent 侧解析为真实路径。
    # '::root' → Windows 盘符列表 / Unix 文件系统根；'::home' → 用户主目录。
    if parent_id == "::root":
        if is_windows:
            return _ok(_list_windows_drives(), "")
        parent_id = "/"
    elif parent_id == "::home":
        parent_id = real_home_dir()

    fallback = fallback_dir or real_home_dir()
    normalized_parent = normalize_platform_path(parent_id) if parent_id else None

    # 非 Windows 平台拒绝 Windows 绝对路径（如 D:\go\src），避免被错误拼接到 cwd 后面。
    if not is_windows and normalized_parent and _is_windows_abs_path(normalized_parent):
        return _fail("path_not_found", f"Directory not found: {parent_id}")

    target = os.path.abspath(normalized_parent) if normalized_parent else fallback

    real_target = os.path.realpath(target)
    if not os.path.exists(real_target):
        return _fail("path_not_found", f"Directory not found: {target}")

    try:
        st = os.stat(real_target)
    except OSError:
        return _fail("path_not_accessible", f"Cannot access path: {target}")

    if not stat_mod.S_ISDIR(st.st_mode):
        # 路径指向文件而非目录，自动解析为其父目录继续浏览。
        real_target = os.path.dirname(real_target)

    try:
        files = _list_directory(
            real_target, show_hidden=show_hidden, allowed_extensions=allowed
        )
    except OSError as exc:
        return _fail("list_failed", str(exc))

    return _ok(files, real_target)


def real_home_dir() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or os.environ.get("USERNAME")
    if user:
        sysname = platform.system()
        if sysname == "Darwin":
            return f"/Users/{user}"
        if sysname == "Linux":
            return f"/home/{user}"
        if sysname == "Windows":
            return f"C:\\Users\\{user}"
    return str(Path.home())


def normalize_platform_path(p: str) -> str:
    """将平台下发的 macOS 风格路径归一化为本机原生路径（仅 Windows 生效）。

    Grix 平台内部使用 macOS 风格路径（如 /Volumes/d/go/src/...）。当 agent 跑在
    Windows 上时，需要先转成 Windows 路径（如 D:\\go\\src\\...）才能被正确解析。
    其它平台原样返回。
    """
    if platform.system() != "Windows":
        return p

    # /Volumes/<letter>/rest... → <LETTER>:\rest...
    m = re.match(r"^/Volumes/([a-zA-Z])(\d*)/(.*)$", p)
    if m:
        drive = m.group(1).upper()
        rest = m.group(3)
        if rest:
            return f"{drive}:\\" + rest.replace("/", "\\")
        return f"{drive}:\\"

    # /Volumes/<letter> 或 /Volumes/<letter><digits>（裸根，无结尾斜杠）
    m = re.match(r"^/Volumes/([a-zA-Z])(\d*)$", p)
    if m:
        return f"{m.group(1).upper()}:\\"

    # /Users/<user>/rest... → C:\Users\<user>\rest...
    m = re.match(r"^/Users/([^/]+)(/.*)?$", p)
    if m:
        rest = m.group(2) or ""
        return f"C:\\Users\\{m.group(1)}" + rest.replace("/", "\\")

    return p


def _list_windows_drives() -> List[Dict[str, Any]]:
    drives: List[Dict[str, Any]] = []
    for code in range(ord("A"), ord("Z") + 1):
        letter = chr(code)
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append({"id": root, "name": f"{letter}:", "is_directory": True})
    return drives


def _list_directory(
    dir_path: str,
    *,
    show_hidden: bool = False,
    allowed_extensions: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []

    for entry in os.scandir(dir_path):
        name = entry.name
        if not show_hidden and name.startswith("."):
            continue

        full_path = os.path.join(dir_path, name)
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        size: Optional[int] = None
        modified_at: Optional[str] = None

        try:
            st = entry.stat(follow_symlinks=True)
            is_dir = stat_mod.S_ISDIR(st.st_mode)
            if not is_dir:
                size = st.st_size
            modified_at = _isoformat(st.st_mtime)
        except OSError:
            # stat 失败（权限不足等）——跳过元数据，但仍保留该条目。
            pass

        if not is_dir and not _matches_allowed_ext(name, allowed_extensions):
            continue

        node: Dict[str, Any] = {
            "id": full_path,
            "name": name,
            "is_directory": is_dir,
        }
        if not is_dir:
            if size is not None:
                node["size"] = size
            mime = _resolve_mime(name)
            if mime:
                node["mime_type"] = mime
        if modified_at is not None:
            node["modified_at"] = modified_at

        nodes.append(node)

    nodes.sort(key=lambda n: (not n["is_directory"], n["name"].lower()))
    return nodes


def _normalize_ext_filter(extensions: Optional[List[Any]]) -> Optional[Set[str]]:
    if not extensions:
        return None
    normalized: Set[str] = set()
    for value in extensions:
        if not isinstance(value, str):
            continue
        v = value.strip().lower()
        if not v:
            continue
        normalized.add(v if v.startswith(".") else f".{v}")
    return normalized or None


def _matches_allowed_ext(name: str, allowed: Optional[Set[str]]) -> bool:
    if allowed is None:
        return True
    dot = name.rfind(".")
    if dot <= 0 or dot >= len(name) - 1:
        return False
    return name[dot:].lower() in allowed


def _resolve_mime(name: str) -> Optional[str]:
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return _EXT_MIME_MAP.get(ext)


def _is_windows_abs_path(p: str) -> bool:
    """判断是否为 Windows 绝对路径（如 C:\\foo 或 \\\\server\\share）。"""
    return bool(re.match(r"^[A-Za-z]:[/\\]", p)) or p.startswith("\\\\")


def _isoformat(timestamp: float) -> str:
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ok(files: List[Dict[str, Any]], current_path: str) -> Dict[str, Any]:
    return {"status": "ok", "result": {"files": files, "current_path": current_path}}


def _fail(error_code: str, error_msg: str) -> Dict[str, str]:
    return {"status": "failed", "error_code": error_code, "error_msg": error_msg}
