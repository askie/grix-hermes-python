"""Atomic JSON file writes with fsync (parity with grix-connector persistence)."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional


def atomic_write_json(file_path: str, snapshot: Any) -> None:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass

    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
        os.replace(str(tmp_path), str(path))
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_unlink(file_path: str) -> None:
    path = Path(file_path)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def read_json_object(file_path: Optional[str]) -> Optional[dict]:
    if not file_path:
        return None
    path = Path(file_path)
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeError("json load failed: path is not a file")
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"json load failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("json load failed: invalid root")
    return parsed
