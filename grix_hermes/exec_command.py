"""Handle /grix exec sub-commands for Hermes adapter."""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Tuple


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
    results: List[SkillEntry] = []
    if not base_dir.is_dir():
        return results
    try:
        for entry in sorted(base_dir.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_file = entry / "SKILL.md"
            if not skill_file.is_file():
                continue
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

    内置 plugin_skills/ 优先于 ~/.hermes/skills/，同名时内置技能遮蔽用户目录项
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
