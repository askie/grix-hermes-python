"""Skill sync-state identification for the toolbar upload button (docs/architecture/39).

Mirrors grix-connector's ``src/core/skill-sync/sync-state.ts``. Compares a skill's
local content digest against the sync manifests (``.grix-sync*.json``，按 owner
隔离，合并读取） that ``skill_syncer.py`` maintains under ``~/.grix/skills`` to
classify each non-managed skill as synced / modified / unsynced. Digest algorithm
matches the backend's ``skillDigest`` exactly: sha256 over the raw SKILL.md text,
no normalization — so a byte-identical local copy always compares equal.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Optional

from .exec_command import SkillEntry
from .skill_paths import read_merged_manifest_skills


def compute_content_digest(content: str) -> str:
    """与后端 service.skillDigest 同规则：sha256(content) 的十六进制。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_manifest_digests(skills_dir: Path) -> Dict[str, str]:
    # 合并全部 owner 的同步台账取 digest 并集（多 owner 宿主机每 owner 一份台账）。
    out: Dict[str, str] = {}
    for name, entry in read_merged_manifest_skills(skills_dir).items():
        if isinstance(entry.get("digest"), str):
            out[name] = entry["digest"]
    return out


def annotate_sync_states(skills: List[SkillEntry], skills_dir: Path) -> List[dict]:
    """给每个非托管技能标注 sync_state；托管技能与读不到内容的技能返回 sync_state=None。

    返回值是纯 dict 列表（而非改 SkillEntry），供上报/序列化直接使用。
    """
    manifest = _read_manifest_digests(skills_dir)
    out: List[dict] = []
    for skill in skills:
        entry = {
            "name": skill.name,
            "description": skill.description,
            "source": skill.source,
            "managed": skill.managed,
            "sync_state": None,
        }
        if not skill.managed and skill.file_path is not None:
            try:
                content = skill.file_path.read_text(encoding="utf-8")
            except Exception:
                content = None
            if content is not None:
                local_digest = compute_content_digest(content)
                remote_digest: Optional[str] = manifest.get(skill.name)
                if remote_digest is None:
                    entry["sync_state"] = "unsynced"
                elif remote_digest == local_digest:
                    entry["sync_state"] = "synced"
                else:
                    entry["sync_state"] = "modified"
        out.append(entry)
    return out
