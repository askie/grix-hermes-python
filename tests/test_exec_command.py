"""Hermes 技能扫描与去重（docs/architecture/39）单元测试。

与 connector 的 dedupeSkills 对齐：同名时先扫描到的内置 plugin_skills/ 优先，
~/.hermes/skills/ 里的同名项被遮蔽，避免平台同步下来的 Grix 内置技能被误标
为非托管并显示同步状态。
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.exec_command import _dedupe_skills, _scan_skill_dir, SkillEntry  # noqa: E402


def _write_skill(base: Path, name: str, description: str = "") -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n", encoding="utf-8")
    return f


def test_dedupe_keeps_first_entry():
    first = SkillEntry(name="grix-admin", description="builtin", source="plugin", managed=True)
    second = SkillEntry(name="grix-admin", description="synced", source="global", managed=False)
    out = _dedupe_skills([first, second])
    assert len(out) == 1
    assert out[0].source == "plugin"
    assert out[0].managed is True
    assert out[0].description == "builtin"


def test_dedupe_keeps_distinct_skills():
    a = SkillEntry(name="a", description="", source="plugin", managed=True)
    b = SkillEntry(name="b", description="", source="global", managed=False)
    out = _dedupe_skills([a, b])
    assert len(out) == 2


def test_dedupe_is_case_insensitive():
    a = SkillEntry(name="Grix-Admin", description="builtin", source="plugin", managed=True)
    b = SkillEntry(name="grix-admin", description="synced", source="global", managed=False)
    out = _dedupe_skills([a, b])
    assert len(out) == 1
    assert out[0].name == "Grix-Admin"


def test_scan_skill_dir_marks_managed_correctly():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, "grix-admin", "builtin")
        entries = _scan_skill_dir(base, "plugin", managed=True)
        assert len(entries) == 1
        assert entries[0].managed is True
        assert entries[0].source == "plugin"


def test_scan_skill_dir_skips_hidden_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        _write_skill(base, ".hidden", "secret")
        entries = _scan_skill_dir(base, "global", managed=False)
        assert len(entries) == 0
