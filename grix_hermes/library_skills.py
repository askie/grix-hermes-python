"""扫描技能库与启用状态（对齐 grix-connector library-skills.ts）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from .exec_command import _parse_skill_frontmatter
from .skill_enable import compute_enable_scope, is_system_skill_entry
from .skill_enable_roots import resolve_enable_roots
from .skill_paths import read_merged_manifest_skills, resolve_library_skills_dir


def _read_manifest(skills_dir: Path) -> Dict[str, dict]:
    # 合并全部 owner 的同步台账（.grix-sync*.json）：库上报对多 owner 技能取并集。
    return read_merged_manifest_skills(skills_dir)


def _read_description(skills_dir: Path, dir_name: str) -> str:
    try:
        content = (skills_dir / dir_name / "SKILL.md").read_text(encoding="utf-8")
        return str(_parse_skill_frontmatter(content).get("description") or "")
    except Exception:
        return ""


def list_library_skills(
    *,
    skills_dir: Optional[Path] = None,
    home: Optional[Path] = None,
    cwd: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """组装上报用的 library_skills（含 global/project enable_scopes）。"""
    lib_dir = skills_dir or resolve_library_skills_dir()
    home_path = home or Path.home()
    roots = resolve_enable_roots(home=home_path, cwd=cwd)
    manifest = _read_manifest(lib_dir)
    out: List[Dict[str, Any]] = []

    for name, entry in sorted(manifest.items()):
        if not name or not isinstance(entry, dict):
            continue
        system = is_system_skill_entry(entry)
        digest = str(entry.get("digest") or "")
        dir_name = str(entry.get("dir") or "")
        item: Dict[str, Any] = {
            "name": name,
            "description": _read_description(lib_dir, dir_name),
            "digest": digest,
            "dir": dir_name,
            "system": system,
            "enable_scopes": {
                "global": compute_enable_scope(
                    name=name,
                    target_root=roots.global_root,
                    system=system,
                    source_digest=digest,
                ),
                "project": compute_enable_scope(
                    name=name,
                    target_root=roots.project_root,
                    system=system,
                    source_digest=digest,
                ),
            },
        }
        owner_id = entry.get("owner_id")
        if owner_id is not None and str(owner_id) != "":
            item["owner_id"] = str(owner_id)
        out.append(item)
    return out
