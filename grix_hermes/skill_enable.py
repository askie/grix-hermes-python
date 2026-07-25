"""技能库启用到 Hermes：软链 enable/disable（对齐 grix-connector skill-enable.ts）。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path
from typing import Dict, Literal, Optional, Tuple

from .exec_command import _parse_skill_frontmatter
from .skill_enable_roots import resolve_enable_roots
from .skill_paths import MANIFEST_FILE, resolve_library_skills_dir
from .skill_sync_state import compute_content_digest

EnableScopeStatus = Literal["none", "link", "unmanaged", "conflict", "broken", "blocked"]
EnableScopeReport = Literal[
    "none", "link", "unmanaged", "conflict", "broken", "blocked", "unavailable"
]
EnableScopeName = Literal["global", "project"]
EnableForce = Literal["replace_link", "replace_with_link"]

MANAGED_MARKER = ".grix-managed"


class SkillEnableError(Exception):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def is_reserved_name(name: str) -> bool:
    return name.strip().lower().startswith("grix-")


def is_system_skill_entry(entry: Optional[dict]) -> bool:
    if not entry:
        return False
    if entry.get("system") is True:
        return True
    # 兼容字符串 "0" 与 JSON 数字 0（两端约定以字符串为主，防御性处理）。
    return str(entry.get("owner_id") if entry.get("owner_id") is not None else "") == "0"


def _read_manifest(skills_dir: Path) -> Dict[str, dict]:
    try:
        import json

        raw = (skills_dir / MANIFEST_FILE).read_text(encoding="utf-8")
        parsed = json.loads(raw)
        skills = parsed.get("skills") if isinstance(parsed, dict) else None
        if isinstance(skills, dict):
            return {k: v for k, v in skills.items() if isinstance(v, dict)}
    except Exception:
        pass
    return {}


def _read_dir_digest(dir_path: Path) -> Optional[str]:
    try:
        content = (dir_path / "SKILL.md").read_text(encoding="utf-8")
        return compute_content_digest(content)
    except Exception:
        return None


def compute_enable_scope(
    *,
    name: str,
    target_root: Optional[Path],
    system: bool,
    source_digest: str,
) -> EnableScopeReport:
    if target_root is None:
        return "unavailable"
    if is_reserved_name(name) or system:
        return "blocked"

    target_path = target_root / name
    if not target_path.exists() and not target_path.is_symlink():
        return "none"

    try:
        if target_path.is_symlink():
            try:
                real = target_path.resolve(strict=True)
            except Exception:
                return "broken"
            if (real / MANAGED_MARKER).exists():
                return "blocked"
            return "link"
        if target_path.is_dir():
            if (target_path / MANAGED_MARKER).exists():
                return "blocked"
            digest = _read_dir_digest(target_path)
            if digest is None:
                return "conflict"
            return "unmanaged" if digest == source_digest else "conflict"
    except OSError:
        return "conflict"
    return "conflict"


def _is_linked_to_source(target_path: Path, source_dir: Path) -> bool:
    try:
        return target_path.resolve() == source_dir.resolve()
    except Exception:
        return False


def _create_skill_link(source_dir: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Windows：目录 junction，避免普通 symlink 需要管理员权限。
        os.symlink(str(source_dir), str(target_path), target_is_directory=True)
    else:
        os.symlink(str(source_dir), str(target_path), target_is_directory=True)


def _remove_skill_link(target_path: Path) -> None:
    if os.name == "nt":
        # junction 用 rmdir；普通 symlink 用 unlink。
        try:
            os.rmdir(target_path)
            return
        except OSError:
            pass
    target_path.unlink()


_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(scope: str, name: str) -> asyncio.Lock:
    key = f"{scope}:{name}"
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def _load_source(
    skills_dir: Path, name: str, entry: dict
) -> Tuple[Path, str]:
    source_dir = skills_dir / str(entry.get("dir") or "")
    try:
        content = (source_dir / "SKILL.md").read_text(encoding="utf-8")
    except Exception as exc:
        raise SkillEnableError(
            f'failed to read library skill "{name}": {exc}', "SKILL_FILE_MISSING"
        ) from exc
    parsed = _parse_skill_frontmatter(content)
    if parsed.get("name") != name:
        raise SkillEnableError(
            f'frontmatter name "{parsed.get("name")}" does not match library skill name "{name}"',
            "NAME_MISMATCH",
        )
    return source_dir, compute_content_digest(content)


def _resolve_target_root(
    *, scope: EnableScopeName, home: Path, cwd: Optional[str]
) -> Path:
    roots = resolve_enable_roots(home=home, cwd=cwd)
    target = roots.global_root if scope == "global" else roots.project_root
    if target is None:
        raise SkillEnableError(
            (
                "project scope unavailable: no session working directory is bound"
                if scope == "project"
                else "skill enable is not supported for this agent mode"
            ),
            "SCOPE_UNAVAILABLE",
        )
    return target


async def enable_skill(
    *,
    name: str,
    scope: str,
    skills_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    cwd: Optional[str] = None,
    force: Optional[str] = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise SkillEnableError("name is required", "MISSING_SKILL_NAME")
    if scope not in ("global", "project"):
        raise SkillEnableError(f"invalid scope: {scope}", "INVALID_SCOPE")
    scope_name: EnableScopeName = scope  # type: ignore[assignment]
    lib_dir = skills_dir or resolve_library_skills_dir()
    home_path = home or Path.home()

    async with _lock_for(scope_name, name):
        manifest = _read_manifest(lib_dir)
        entry = manifest.get(name)
        if not entry:
            raise SkillEnableError(f'skill "{name}" not found in library', "SKILL_NOT_FOUND")
        source_dir, digest = _load_source(lib_dir, name, entry)

        if is_reserved_name(name):
            raise SkillEnableError(
                f'"{name}" uses the reserved "grix-" prefix and cannot be enabled',
                "BLOCKED",
            )
        system = is_system_skill_entry(entry)
        if system:
            raise SkillEnableError(
                f'"{name}" is a platform system skill and cannot be enabled',
                "BLOCKED",
            )

        target_root = _resolve_target_root(scope=scope_name, home=home_path, cwd=cwd)
        target_path = target_root / name
        status = compute_enable_scope(
            name=name, target_root=target_root, system=system, source_digest=digest
        )

        if status == "blocked":
            raise SkillEnableError(
                f'"{name}" target slot is managed by the connector', "BLOCKED"
            )
        if status == "conflict":
            raise SkillEnableError(
                f'"{name}" already exists locally with different content and cannot be overwritten',
                "CONFLICT",
            )
        if status == "unmanaged" and force != "replace_with_link":
            raise SkillEnableError(
                f'"{name}" already exists locally as a real directory; pass force="replace_with_link" to overwrite',
                "NEEDS_FORCE",
            )
        if status == "link":
            if _is_linked_to_source(target_path, source_dir):
                return {
                    "name": name,
                    "scope": scope_name,
                    "status": "link",
                    "path": str(target_path),
                    "changed": False,
                }
            if force != "replace_link":
                raise SkillEnableError(
                    f'"{name}" is already linked elsewhere; pass force="replace_link" to overwrite',
                    "NEEDS_FORCE",
                )

        if status in ("broken", "link"):
            _remove_skill_link(target_path)
        elif status == "unmanaged":
            shutil.rmtree(target_path, ignore_errors=True)
        _create_skill_link(source_dir, target_path)
        return {
            "name": name,
            "scope": scope_name,
            "status": "link",
            "path": str(target_path),
            "changed": True,
        }


async def disable_skill(
    *,
    name: str,
    scope: str,
    home: Optional[Path] = None,
    cwd: Optional[str] = None,
) -> dict:
    name = (name or "").strip()
    if not name:
        raise SkillEnableError("name is required", "MISSING_SKILL_NAME")
    if scope not in ("global", "project"):
        raise SkillEnableError(f"invalid scope: {scope}", "INVALID_SCOPE")
    scope_name: EnableScopeName = scope  # type: ignore[assignment]
    home_path = home or Path.home()

    async with _lock_for(scope_name, name):
        target_root = _resolve_target_root(scope=scope_name, home=home_path, cwd=cwd)
        target_path = target_root / name
        if not target_path.exists() and not target_path.is_symlink():
            return {
                "name": name,
                "scope": scope_name,
                "path": str(target_path),
                "removed": False,
            }
        if not target_path.is_symlink():
            raise SkillEnableError(
                f'"{name}" at {target_path} is a real directory, not a managed link, and cannot be disabled',
                "NOT_MANAGED",
            )
        _remove_skill_link(target_path)
        return {
            "name": name,
            "scope": scope_name,
            "path": str(target_path),
            "removed": True,
        }
