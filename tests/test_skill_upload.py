"""工具栏一键上传（docs/architecture/39 §4）单元测试。

与 grix-connector tests/skill-upload.test.ts 覆盖面逐条对齐：找不到/系统托管拒绝、
成功路径带鉴权头与技能全文、后端非 0 code 视为失败、HTTP/传输错误视为失败。
"""

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.exec_command import SkillEntry  # noqa: E402
from grix_hermes import skill_upload as skill_upload_module  # noqa: E402
from grix_hermes.skill_upload import SkillUploadError, find_uploadable_skill, upload_skill  # noqa: E402


def _write_skill(base: Path, name: str, content: str) -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(content, encoding="utf-8")
    return f


def test_finds_non_managed_skill():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _write_skill(Path(tmp), "我的技能", "d")
        entries = [SkillEntry(name="我的技能", description="d", source="global", managed=False, file_path=fp)]
        hit = find_uploadable_skill("我的技能", entries)
        assert hit.name == "我的技能"


def test_rejects_managed_skill_even_if_name_matches():
    with tempfile.TemporaryDirectory() as tmp:
        fp = _write_skill(Path(tmp), "系统技能", "d")
        entries = [SkillEntry(name="系统技能", description="", source="plugin", managed=True, file_path=fp)]
        with pytest.raises(SkillUploadError, match="system-managed"):
            find_uploadable_skill("系统技能", entries)


def test_missing_skill_raises_not_found():
    with pytest.raises(SkillUploadError, match="not found"):
        find_uploadable_skill("不存在", [])


def test_blank_name_raises_required():
    with pytest.raises(SkillUploadError, match="required"):
        find_uploadable_skill("   ", [])


def test_success_posts_name_and_content_with_auth_header(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        fp = _write_skill(Path(tmp), "我的技能", "# 内容")
        entries = [SkillEntry(name="我的技能", description="", source="global", managed=False, file_path=fp)]
        monkeypatch.setattr(skill_upload_module, "scan_hermes_skills", lambda: entries)

        calls = []

        async def fake_post(url, api_key, body):
            calls.append((url, api_key, body))
            return {"code": 0}

        asyncio.run(
            upload_skill(
                "我的技能",
                "test-key",
                "ws://127.0.0.1:27189/v1/agent-api/ws?agent_id=1",
                post_json=fake_post,
            )
        )
        assert len(calls) == 1
        url, api_key, body = calls[0]
        assert url == "http://127.0.0.1:27189/v1/agent-api/skills/upload"
        assert api_key == "test-key"
        assert body == {"name": "我的技能", "content": "# 内容"}


def test_backend_nonzero_code_is_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        fp = _write_skill(Path(tmp), "a", "x")
        entries = [SkillEntry(name="a", description="", source="global", managed=False, file_path=fp)]
        monkeypatch.setattr(skill_upload_module, "scan_hermes_skills", lambda: entries)

        async def fake_post(url, api_key, body):
            assert url == "http://h/v1/agent-api/skills/upload"
            assert api_key == "k"
            assert body == {"name": "a", "content": "x"}
            return {"code": 1, "msg": "重名"}

        with pytest.raises(SkillUploadError, match="重名"):
            asyncio.run(upload_skill("a", "k", "ws://h/v1/agent-api/ws", post_json=fake_post))


def test_transport_error_is_wrapped(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        fp = _write_skill(Path(tmp), "a", "x")
        entries = [SkillEntry(name="a", description="", source="global", managed=False, file_path=fp)]
        monkeypatch.setattr(skill_upload_module, "scan_hermes_skills", lambda: entries)

        async def failing_post(url, api_key, body):
            raise RuntimeError("HTTP 500")

        with pytest.raises(SkillUploadError, match="upload request failed"):
            asyncio.run(upload_skill("a", "k", "ws://h/v1/agent-api/ws", post_json=failing_post))


def test_managed_skill_rejected_before_any_request(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        fp = _write_skill(Path(tmp), "系统技能", "x")
        entries = [SkillEntry(name="系统技能", description="", source="plugin", managed=True, file_path=fp)]
        monkeypatch.setattr(skill_upload_module, "scan_hermes_skills", lambda: entries)
        called = False

        async def should_not_be_called(url, api_key, body):
            nonlocal called
            called = True
            return {"code": 0}

        with pytest.raises(SkillUploadError, match="system-managed"):
            asyncio.run(
                upload_skill("系统技能", "k", "ws://h/v1/agent-api/ws", post_json=should_not_be_called)
            )
        assert called is False
