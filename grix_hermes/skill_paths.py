"""技能库路径约定（对齐 grix-connector GRIX_PATHS.skills）。

库副本与 connector 共用 ``~/.grix/skills``（可用 ``GRIX_CONNECTOR_HOME`` 覆盖根目录），
启用主根仍是 Hermes 扫描目录 ``~/.hermes/skills``。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

GRIX_HOME_ENV = "GRIX_CONNECTOR_HOME"
MANIFEST_FILE = ".grix-sync.json"


def resolve_grix_home() -> Path:
    override = (os.environ.get(GRIX_HOME_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".grix"


def resolve_library_skills_dir() -> Path:
    """平台技能库本机落盘目录（SkillSyncer 真源）。"""
    return resolve_grix_home() / "skills"


def resolve_hermes_enable_global(*, home: Optional[Path] = None) -> Path:
    """Hermes 全局启用主根（scan_hermes_skills 用户级目录）。"""
    return (home or Path.home()) / ".hermes" / "skills"


def migrate_legacy_hermes_library(
    *,
    library_dir: Optional[Path] = None,
    legacy_dir: Optional[Path] = None,
) -> bool:
    """把旧版落在 ``~/.hermes/skills`` 的同步台账迁到 ``~/.grix/skills``。

    条件：库目录尚无台账、旧目录有台账。迁移后把已迁技能以同名软链挂回 Hermes
    启用根，保持「已可见」行为，后续由用户/工具栏按需 disable。

    返回是否实际执行了迁移。
    """
    lib = library_dir or resolve_library_skills_dir()
    legacy = legacy_dir or resolve_hermes_enable_global()
    lib_manifest = lib / MANIFEST_FILE
    legacy_manifest = legacy / MANIFEST_FILE
    if lib_manifest.exists() or not legacy_manifest.exists():
        return False

    try:
        parsed = json.loads(legacy_manifest.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[skill-sync] legacy manifest unreadable, skip migrate: %s", exc)
        return False
    skills = parsed.get("skills") if isinstance(parsed, dict) else None
    if not isinstance(skills, dict) or not skills:
        return False

    lib.mkdir(parents=True, exist_ok=True)
    moved = 0
    for name, entry in list(skills.items()):
        if not isinstance(entry, dict):
            continue
        dir_name = str(entry.get("dir") or "").strip()
        if not dir_name or not name:
            continue
        src = legacy / dir_name
        dst = lib / dir_name
        try:
            if src.is_symlink():
                # 已是链：尽量把目标拷进库，再重建为指向库的链。
                try:
                    real = src.resolve(strict=True)
                except Exception:
                    continue
                if real.exists() and real.is_dir():
                    if not dst.exists():
                        shutil.copytree(real, dst)
                    src.unlink()
                    os.symlink(str(dst), str(src), target_is_directory=True)
                    moved += 1
                continue
            if not src.is_dir():
                continue
            if dst.exists():
                shutil.rmtree(src, ignore_errors=True)
            else:
                shutil.move(str(src), str(dst))
            link_path = legacy / name
            # 不覆盖用户自建真实目录/已有链接。
            if link_path.exists() or link_path.is_symlink():
                continue
            os.symlink(str(dst), str(link_path), target_is_directory=True)
            moved += 1
        except OSError as exc:
            logger.warning("[skill-sync] migrate skill %r failed: %s", name, exc)

    try:
        lib_manifest.write_text(
            json.dumps({"skills": skills}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        legacy_manifest.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("[skill-sync] write migrated manifest failed: %s", exc)
        return False

    logger.info(
        "[skill-sync] migrated %d skill(s) from %s -> %s",
        moved,
        legacy,
        lib,
    )
    return True
