"""技能同步状态识别（docs/architecture/39 §3）单元测试。

与 grix-connector tests/sync-state.test.ts 覆盖面逐条对齐：三态判定、
系统托管技能永不参与判定、digest 算法与后端 skillDigest 对齐、无台账/读文件失败兜底。
"""

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.exec_command import SkillEntry  # noqa: E402
from grix_hermes.skill_sync_state import annotate_sync_states, compute_content_digest  # noqa: E402


def test_digest_matches_backend_sha256():
    content = "# 示例技能\n这是内容"
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert compute_content_digest(content) == expected


def _write_skill(base: Path, name: str, content: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    return f


def _write_manifest(base: Path, skills: dict) -> None:
    (base / ".grix-sync.json").write_text(json.dumps({"skills": skills}), encoding="utf-8")


def test_no_manifest_entry_is_unsynced():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fp = _write_skill(base, "自建技能", "内容 A")
        entries = [SkillEntry(name="自建技能", description="", source="global", managed=False, file_path=fp)]
        out = annotate_sync_states(entries, base)
        assert out[0]["sync_state"] == "unsynced"


def test_matching_digest_is_synced():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        content = "内容 B"
        fp = _write_skill(base, "已同步技能", content)
        _write_manifest(base, {"已同步技能": {"digest": compute_content_digest(content)}})
        entries = [SkillEntry(name="已同步技能", description="", source="global", managed=False, file_path=fp)]
        out = annotate_sync_states(entries, base)
        assert out[0]["sync_state"] == "synced"


def test_mismatched_digest_is_modified():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fp = _write_skill(base, "改过技能", "本地新内容")
        _write_manifest(base, {"改过技能": {"digest": compute_content_digest("库里旧内容")}})
        entries = [SkillEntry(name="改过技能", description="", source="global", managed=False, file_path=fp)]
        out = annotate_sync_states(entries, base)
        assert out[0]["sync_state"] == "modified"


def test_managed_skill_never_gets_sync_state_even_if_manifest_has_matching_name():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fp = _write_skill(base, "系统技能", "内容")
        _write_manifest(base, {"系统技能": {"digest": "unrelated"}})
        entries = [SkillEntry(name="系统技能", description="", source="plugin", managed=True, file_path=fp)]
        out = annotate_sync_states(entries, base)
        assert out[0]["sync_state"] is None
        assert out[0]["managed"] is True


def test_unreadable_file_leaves_sync_state_none_without_crashing():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        entries = [
            SkillEntry(
                name="坏路径技能",
                description="",
                source="global",
                managed=False,
                file_path=base / "不存在" / "SKILL.md",
            )
        ]
        out = annotate_sync_states(entries, base)
        assert out[0]["sync_state"] is None


def test_missing_manifest_file_treats_all_as_unsynced():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        fp = _write_skill(base, "任意技能", "内容")
        entries = [SkillEntry(name="任意技能", description="", source="global", managed=False, file_path=fp)]
        out = annotate_sync_states(entries, base)
        assert out[0]["sync_state"] == "unsynced"
