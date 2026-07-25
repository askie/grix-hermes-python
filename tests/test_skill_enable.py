"""skill_enable / library_skills（对齐 connector skill-enable / library-skills）。"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.library_skills import list_library_skills  # noqa: E402
from grix_hermes.skill_enable import (  # noqa: E402
    SkillEnableError,
    disable_skill,
    enable_skill,
)
from grix_hermes.skill_paths import migrate_legacy_hermes_library  # noqa: E402
from grix_hermes.skill_sync_state import compute_content_digest  # noqa: E402


def _fm(name: str, desc: str = "d") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n"


def _write_lib(skills_dir: Path, name: str, *, owner_id: str = "1", content: str | None = None) -> str:
    text = content if content is not None else _fm(name)
    digest = compute_content_digest(text)
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(text, encoding="utf-8")
    manifest_path = skills_dir / ".grix-sync.json"
    manifest = {"skills": {}}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["skills"][name] = {
        "id": "1",
        "version": "1",
        "digest": digest,
        "dir": name,
        "owner_id": owner_id,
        "system": owner_id == "0",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return digest


def test_enable_creates_symlink_and_disable_removes():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        lib = base / "lib"
        home = base / "home"
        lib.mkdir()
        home.mkdir()
        _write_lib(lib, "demo")

        result = asyncio.run(
            enable_skill(name="demo", scope="global", skills_dir=lib, home=home)
        )
        assert result["changed"] is True
        target = home / ".hermes" / "skills" / "demo"
        assert target.is_symlink()
        assert target.resolve() == (lib / "demo").resolve()

        # 幂等
        again = asyncio.run(
            enable_skill(name="demo", scope="global", skills_dir=lib, home=home)
        )
        assert again["changed"] is False

        disabled = asyncio.run(disable_skill(name="demo", scope="global", home=home))
        assert disabled["removed"] is True
        assert not target.exists() and not target.is_symlink()


def test_system_skill_blocked():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        lib = base / "lib"
        home = base / "home"
        lib.mkdir()
        home.mkdir()
        _write_lib(lib, "sys", owner_id="0")
        try:
            asyncio.run(enable_skill(name="sys", scope="global", skills_dir=lib, home=home))
            raise AssertionError("expected SkillEnableError")
        except SkillEnableError as exc:
            assert exc.code == "BLOCKED"


def test_conflict_hard_rejects_even_with_force():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        lib = base / "lib"
        home = base / "home"
        lib.mkdir()
        home.mkdir()
        _write_lib(lib, "demo")
        slot = home / ".hermes" / "skills" / "demo"
        slot.mkdir(parents=True)
        (slot / "SKILL.md").write_text(_fm("demo", "other"), encoding="utf-8")
        try:
            asyncio.run(
                enable_skill(
                    name="demo",
                    scope="global",
                    skills_dir=lib,
                    home=home,
                    force="replace_with_link",
                )
            )
            raise AssertionError("expected CONFLICT")
        except SkillEnableError as exc:
            assert exc.code == "CONFLICT"


def test_project_scope_unavailable_without_cwd():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        lib = base / "lib"
        home = base / "home"
        lib.mkdir()
        home.mkdir()
        _write_lib(lib, "demo")
        try:
            asyncio.run(
                enable_skill(name="demo", scope="project", skills_dir=lib, home=home, cwd=None)
            )
            raise AssertionError("expected SCOPE_UNAVAILABLE")
        except SkillEnableError as exc:
            assert exc.code == "SCOPE_UNAVAILABLE"


def test_list_library_skills_enable_scopes():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        lib = base / "lib"
        home = base / "home"
        cwd = base / "proj"
        lib.mkdir()
        home.mkdir()
        cwd.mkdir()
        _write_lib(lib, "demo")
        items = list_library_skills(skills_dir=lib, home=home, cwd=str(cwd))
        assert len(items) == 1
        assert items[0]["name"] == "demo"
        assert items[0]["enable_scopes"]["global"] == "none"
        assert items[0]["enable_scopes"]["project"] == "none"
        assert "system" in items[0]

        asyncio.run(enable_skill(name="demo", scope="global", skills_dir=lib, home=home))
        items2 = list_library_skills(skills_dir=lib, home=home, cwd=str(cwd))
        assert items2[0]["enable_scopes"]["global"] == "link"


def test_migrate_legacy_hermes_library():
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        legacy = base / "hermes" / "skills"
        lib = base / "grix" / "skills"
        legacy.mkdir(parents=True)
        content = _fm("old")
        (legacy / "old").mkdir()
        (legacy / "old" / "SKILL.md").write_text(content, encoding="utf-8")
        digest = compute_content_digest(content)
        (legacy / ".grix-sync.json").write_text(
            json.dumps(
                {
                    "skills": {
                        "old": {
                            "id": "9",
                            "version": "1",
                            "digest": digest,
                            "dir": "old",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        assert migrate_legacy_hermes_library(library_dir=lib, legacy_dir=legacy)
        assert (lib / "old" / "SKILL.md").exists()
        assert (lib / ".grix-sync.json").exists()
        assert not (legacy / ".grix-sync.json").exists()
        link = legacy / "old"
        assert link.is_symlink()
        assert link.resolve() == (lib / "old").resolve()
        # 库已有台账 → 不再迁
        assert migrate_legacy_hermes_library(library_dir=lib, legacy_dir=legacy) is False
