"""Grix file-list local action handler."""

from __future__ import annotations

import mimetypes
import os
import platform
import stat as stat_mod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .protocol import get_hostname


def handle_file_list_action(
    params: Dict[str, Any],
    *,
    resolve_cwd: Callable[[str], Optional[str]],
    fallback_dir: Optional[str] = None,
) -> Dict[str, Any]:
    parent_id = params.get("parent_id") or None
    session_id = params.get("session_id") or ""
    show_hidden = bool(params.get("show_hidden"))

    cwd = resolve_cwd(session_id) if session_id else None
    target = parent_id or cwd or fallback_dir or real_home_dir()

    if not target:
        return _fail("path_not_found", "No directory to list")

    try:
        real_target = os.path.realpath(target)
    except OSError:
        return _fail("path_not_found", f"Directory not found: {target}")

    if cwd:
        try:
            real_cwd = os.path.realpath(cwd)
        except OSError:
            real_cwd = None
        if real_cwd and not _is_within_path(real_target, real_cwd):
            return _fail(
                "path_outside_cwd",
                "Requested path is outside session working directory",
            )

    try:
        st = os.stat(real_target)
        if not stat_mod.S_ISDIR(st.st_mode):
            # 路径指向文件而非目录，自动解析为其父目录继续浏览。
            real_target = os.path.dirname(real_target)
    except PermissionError:
        return _fail("path_not_accessible", f"Cannot access path: {target}")
    except OSError:
        return _fail("path_not_found", f"Directory not found: {target}")

    try:
        files = _list_directory(real_target, show_hidden=show_hidden)
    except PermissionError:
        return _fail("path_not_accessible", f"Cannot access path: {real_target}")
    except OSError as exc:
        return _fail("list_failed", str(exc))

    return {
        "status": "ok",
        "result": {
            "files": files,
            "current_path": real_target,
            "machine_name": get_hostname(),
        },
    }


def real_home_dir() -> str:
    user = os.environ.get("USER") or os.environ.get("LOGNAME")
    if user:
        sys = platform.system()
        if sys == "Darwin":
            return f"/Users/{user}"
        if sys == "Linux":
            return f"/home/{user}"
        if sys == "Windows":
            return os.path.expandvars(rf"C:\Users\{user}")
    return str(Path.home())


def _list_directory(dir_path: str, *, show_hidden: bool = False) -> List[Dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        dir_entries = list(os.scandir(dir_path))
    except OSError:
        return []

    for entry in dir_entries:
        name = entry.name
        if not show_hidden and name.startswith("."):
            continue

        try:
            st = entry.stat(follow_symlinks=True)
            is_dir = stat_mod.S_ISDIR(st.st_mode)
        except OSError:
            continue

        node: Dict[str, Any] = {
            "id": os.path.join(dir_path, name),
            "name": name,
            "is_directory": is_dir,
        }
        if not is_dir:
            node["size"] = st.st_size
            node["modified_at"] = _isoformat(st.st_mtime)
            mime = _guess_mime(name)
            if mime:
                node["mime_type"] = mime
        else:
            node["modified_at"] = _isoformat(st.st_mtime)

        entries.append(node)

    dirs = sorted(
        (e for e in entries if e["is_directory"]),
        key=lambda e: e["name"].lower(),
    )
    files = sorted(
        (e for e in entries if not e["is_directory"]),
        key=lambda e: e["name"].lower(),
    )
    return dirs + files


def _is_within_path(target: str, ancestor: str) -> bool:
    if target == ancestor:
        return True
    sep = os.sep
    norm_target = target if target.endswith(sep) else target + sep
    norm_ancestor = ancestor if ancestor.endswith(sep) else ancestor + sep
    return norm_target.startswith(norm_ancestor)


def _guess_mime(name: str) -> Optional[str]:
    mime, _ = mimetypes.guess_type(name)
    return mime or None


def _isoformat(timestamp: float) -> str:
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _fail(error_code: str, error_msg: str) -> Dict[str, str]:
    return {"status": "failed", "error_code": error_code, "error_msg": error_msg}
