"""Handle /grix exec sub-commands for Hermes adapter."""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional, Tuple

# 与 Hermes ``agent.skill_utils`` / connector ``scanSkillTree`` 对齐：Hermes 技能
# 官方布局是 ``category/skill/SKILL.md``，必须递归发现；同时跳过归档、依赖与
# progressive-disclosure 支持目录，避免把 references/ 里的归档包当成独立技能。
_EXCLUDED_SKILL_DIRS = frozenset(
    {
        ".git",
        ".github",
        ".hub",
        ".archive",
        ".venv",
        "venv",
        "node_modules",
        "site-packages",
        "__pycache__",
        ".tox",
        ".nox",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)
_SKILL_SUPPORT_DIRS = frozenset({"references", "templates", "assets", "scripts"})
_MAX_SKILL_DEPTH = 6


class SkillEntry:
    __slots__ = ("name", "description", "source", "managed", "file_path")

    def __init__(
        self,
        name: str,
        description: str,
        source: str,
        managed: bool = False,
        file_path: Optional[Path] = None,
    ) -> None:
        self.name = name
        self.description = description
        self.source = source
        # 系统托管技能（package-bundled plugin_skills/）不可一键上传，见 docs/architecture/39。
        self.managed = managed
        self.file_path = file_path


def parse_exec_command(text: str) -> Optional[Tuple[str, str]]:
    """Parse '/grix exec <subcommand> [args]' from text.

    Returns (subcommand, args) or None.
    """
    tokens = str(text or "").strip().split()
    if len(tokens) < 3:
        return None
    if tokens[0].lower() not in ("/grix", "grix"):
        return None
    if tokens[1].lower() != "exec":
        return None
    return tokens[2].lower(), " ".join(tokens[3:])


def _parse_skill_frontmatter(content: str) -> dict:
    trimmed = content.strip()
    if not trimmed.startswith("---"):
        return {"name": "", "description": ""}
    end_idx = trimmed.find("---", 3)
    if end_idx == -1:
        return {"name": "", "description": ""}
    frontmatter = trimmed[3:end_idx].strip()
    result = {"name": "", "description": ""}
    for line in frontmatter.split("\n"):
        colon_idx = line.find(":")
        if colon_idx == -1:
            continue
        key = line[:colon_idx].strip()
        value = line[colon_idx + 1 :].strip().strip("\"'")
        if key == "name":
            result["name"] = value
        elif key == "description":
            result["description"] = value
    return result


def _scan_skill_dir(base_dir: Path, source: str, *, managed: bool = False) -> List[SkillEntry]:
    """递归扫描 ``base_dir`` 下所有含 SKILL.md 的技能包。

    支持 Hermes 分类布局（如 ``software-development/camoufox/SKILL.md``），
    深度上限与 connector ``scanSkillTree`` 一致（默认 6）。
    """
    results: List[SkillEntry] = []
    if not base_dir.is_dir():
        return results
    base_str = str(base_dir)
    try:
        for root, dirs, files in os.walk(base_str, followlinks=True):
            rel = os.path.relpath(root, base_str)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            has_skill_md = "SKILL.md" in files
            # 就地剪枝：隐藏目录、Hermes 排除集、技能包内 support 目录、深度上限。
            dirs[:] = sorted(
                d
                for d in dirs
                if not d.startswith(".")
                and d not in _EXCLUDED_SKILL_DIRS
                and not (has_skill_md and d in _SKILL_SUPPORT_DIRS)
                and depth < _MAX_SKILL_DEPTH
            )
            if not has_skill_md:
                continue
            # 技能根本身不作为技能包（与旧版「只看子目录」一致）。
            if rel == ".":
                continue
            skill_file = Path(root) / "SKILL.md"
            try:
                parsed = _parse_skill_frontmatter(skill_file.read_text(encoding="utf-8"))
                if parsed["name"]:
                    results.append(
                        SkillEntry(
                            name=parsed["name"],
                            description=parsed["description"],
                            source=source,
                            managed=managed,
                            file_path=skill_file,
                        )
                    )
            except Exception:
                pass
    except Exception:
        pass
    return results


def _dedupe_skills(entries: List[SkillEntry]) -> List[SkillEntry]:
    """按名去重，保留先出现者。

    与 connector dedupeSkills 对齐：同名时先扫描到的条目优先。plugin_skills/
    先于 ~/.hermes/skills/ 扫描，因此内置 Grix 技能会遮蔽用户目录里的同名项，
    避免平台同步下来的同名技能被误当作用户自建技能参与上传/同步状态显示。
    """
    out: List[SkillEntry] = []
    seen = set()
    for entry in entries:
        key = entry.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def scan_hermes_skills() -> List[SkillEntry]:
    """Scan Hermes skill directories for SKILL.md files.

    递归扫描以覆盖 Hermes 官方 ``category/skill/SKILL.md`` 布局。内置
    plugin_skills/ 优先于 ~/.hermes/skills/，同名时内置技能遮蔽用户目录项
    （dedupe），防止平台同步下来的 Grix 内置技能被误标为非托管并显示同步状态。
    """
    results: List[SkillEntry] = []

    # Package-bundled plugin_skills/：系统自带，不可一键上传。
    package_dir = Path(__file__).parent / "plugin_skills"
    results.extend(_scan_skill_dir(package_dir, "plugin", managed=True))

    # User-level ~/.hermes/skills/：用户/agent 自建，可一键上传。
    home_skills = Path.home() / ".hermes" / "skills"
    results.extend(_scan_skill_dir(home_skills, "global", managed=False))

    return _dedupe_skills(results)


def handle_skills_command() -> str:
    skills = scan_hermes_skills()
    if not skills:
        return "No skills found."
    lines = []
    for i, s in enumerate(skills, 1):
        lines.append(f"{i}. {s.name} [{s.source}]\n   {s.description}")
    return f"Found {len(skills)} skill(s):\n\n" + "\n\n".join(lines)
