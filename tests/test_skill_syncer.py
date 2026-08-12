"""SkillSyncer（自定义技能多机同步，docs/architecture/38）单元测试。

与 grix-connector tests/skill-syncer.test.ts 的覆盖面逐条对齐：
首次同步落盘、digest 不变不重拉、平台删除清理、不可达不删本地、
非法名净化与碰撞哈希、缺 content 字段跳过、触发补轮、stop 清位。
owner 级改造新增：多凭证取首个可达、台账无变化不写盘不回调、
digest 命中回填后不振荡、跨 owner 台账隔离与删除保护、
owner syncer 首次 start 收养并删除旧版 .grix-sync.json、台账原子写。
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.skill_syncer import SkillSyncer, safe_dir_name  # noqa: E402

ENDPOINT = "ws://127.0.0.1:27189/v1/agent-api/ws?agent_id=1"


def make_fetch(list_items: List[Dict[str, Any]], contents: Dict[str, str]):
    """构造按 URL 路由的假 fetch_json：清单与单条内容各返回预置数据。"""

    calls: List[str] = []

    async def fetch(url: str, api_key: str) -> Optional[Dict[str, Any]]:
        calls.append(url)
        if url.endswith("/v1/agent-api/skills"):
            return {"code": 0, "data": {"items": list_items}}
        for sid, content in contents.items():
            if f"/skills/{sid}/content" in url:
                return {"code": 0, "data": {"content": content}}
        return {"code": 1}

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def new_syncer(tmp: Path, fetch, **kw) -> SkillSyncer:
    return SkillSyncer([(ENDPOINT, "k")], skills_dir=tmp, fetch_json=fetch, **kw)


def read_manifest(tmp: Path) -> Dict[str, Any]:
    return json.loads((tmp / ".grix-sync.json").read_text(encoding="utf-8"))


def test_first_sync_writes_skill_and_manifest():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [{"id": "10", "name": "报告规范", "version": "1", "digest": "d1"}],
            {"10": "# 报告规范\n内容"},
        )
        asyncio.run(new_syncer(tmp, fetch).sync_once())

        assert (tmp / "报告规范" / "SKILL.md").read_text(encoding="utf-8") == "# 报告规范\n内容"
        manifest = read_manifest(tmp)
        assert manifest["skills"]["报告规范"]["digest"] == "d1"


def test_unchanged_digest_skips_content_fetch():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "c1"}
        )
        s = new_syncer(tmp, fetch)
        asyncio.run(s.sync_once())
        calls_after_first = len(fetch.calls)
        asyncio.run(s.sync_once())
        # 第二轮只多一次清单调用，不再拉内容。
        assert len(fetch.calls) == calls_after_first + 1


def test_platform_deleted_removes_local_synced_only():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "c1"}
        )
        s = new_syncer(tmp, fetch)
        asyncio.run(s.sync_once())
        assert (tmp / "a").exists()

        # 用户本机自建技能（不在 manifest）不受影响。
        local_dir = tmp / "本地自建"
        local_dir.mkdir()
        (local_dir / "SKILL.md").write_text("mine", encoding="utf-8")

        fetch2 = make_fetch([], {})
        asyncio.run(new_syncer(tmp, fetch2).sync_once())
        assert not (tmp / "a").exists()
        assert (local_dir / "SKILL.md").read_text(encoding="utf-8") == "mine"
        assert read_manifest(tmp)["skills"] == {}


def test_unreachable_platform_never_deletes_local():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "c1"}
        )
        asyncio.run(new_syncer(tmp, fetch).sync_once())

        async def unreachable(url: str, api_key: str):
            return None

        asyncio.run(new_syncer(tmp, unreachable).sync_once())
        assert (tmp / "a" / "SKILL.md").exists()
        assert read_manifest(tmp)["skills"]["a"]["digest"] == "d1"


def test_missing_content_field_skipped_no_empty_file():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        async def fetch(url: str, api_key: str):
            if url.endswith("/v1/agent-api/skills"):
                return {"code": 0, "data": {"items": [
                    {"id": "15", "name": "broken", "version": "1", "digest": "d1"},
                    {"id": "16", "name": "fine", "version": "1", "digest": "d2"},
                ]}}
            if "/skills/15/" in url:
                return {"code": 0, "data": {}}
            return {"code": 0, "data": {"content": "ok"}}

        asyncio.run(new_syncer(tmp, fetch).sync_once())
        assert not (tmp / "broken").exists()
        assert (tmp / "fine" / "SKILL.md").read_text(encoding="utf-8") == "ok"
        manifest = read_manifest(tmp)
        assert "broken" not in manifest["skills"]
        assert manifest["skills"]["fine"]["digest"] == "d2"


def test_safe_dir_name_rules():
    # 与 connector safeDirName 对齐：非法一律拒绝或净化+碰撞哈希。
    assert safe_dir_name("..") is None
    assert safe_dir_name(".hidden") is None or not safe_dir_name(".hidden").startswith(".")
    assert safe_dir_name("con") is None
    assert safe_dir_name("COM1") is None
    assert safe_dir_name("正常名") == "正常名"
    # 净化改名后附加原名 sha1 前 8 位，不同原名不会撞同一目录。
    a = safe_dir_name("a:b")
    b = safe_dir_name("a?b")
    assert a and a.startswith("a_b-") and len(a) == len("a_b-") + 8
    assert b and b.startswith("a_b-") and a != b
    expected = "a_b-" + hashlib.sha1("a:b".encode()).hexdigest()[:8]
    assert a == expected
    # 路径分隔符替换后同样带哈希。
    assert safe_dir_name("x/y") and "/" not in safe_dir_name("x/y")


def test_unsafe_name_skipped_in_sync():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [
                {"id": "20", "name": "..", "version": "1", "digest": "d1"},
                {"id": "21", "name": "好技能", "version": "1", "digest": "d2"},
            ],
            {"20": "evil", "21": "good"},
        )
        asyncio.run(new_syncer(tmp, fetch).sync_once())
        assert (tmp / "好技能" / "SKILL.md").exists()
        assert ".." not in read_manifest(tmp)["skills"]


def test_trigger_during_sync_reruns_after():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        state = {"round": 0}
        gate: asyncio.Event

        async def fetch(url: str, api_key: str):
            if url.endswith("/v1/agent-api/skills"):
                state["round"] += 1
                if state["round"] == 1:
                    await gate.wait()  # 卡住第一轮，让 trigger 在 running 期间到达
                digest = "d1" if state["round"] == 1 else "d2"
                return {"code": 0, "data": {"items": [
                    {"id": "10", "name": "a", "version": str(state["round"]), "digest": digest},
                ]}}
            return {"code": 0, "data": {"content": f"content-r{state['round']}"}}

        async def scenario():
            nonlocal gate
            gate = asyncio.Event()
            s = new_syncer(tmp, fetch)
            first = asyncio.ensure_future(s.sync_once())
            await asyncio.sleep(0.05)  # 让第一轮跑到清单调用并卡在 gate
            s.trigger_sync()  # running 期间的触发 → 记待补轮
            gate.set()
            await first
            # 等补跑那轮完成（异步派发）。
            for _ in range(100):
                await asyncio.sleep(0.02)
                manifest = read_manifest(tmp)
                if manifest["skills"].get("a", {}).get("digest") == "d2":
                    return manifest
            raise AssertionError("rerun round did not converge")

        manifest = asyncio.run(scenario())
        assert manifest["skills"]["a"]["digest"] == "d2"
        assert (tmp / "a" / "SKILL.md").read_text(encoding="utf-8") == "content-r2"


def test_stop_clears_pending_rerun():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        state = {"round": 0}
        gate: asyncio.Event

        async def fetch(url: str, api_key: str):
            if url.endswith("/v1/agent-api/skills"):
                state["round"] += 1
                if state["round"] == 1:
                    await gate.wait()
                return {"code": 0, "data": {"items": []}}
            return {"code": 0, "data": {"content": "x"}}

        async def scenario():
            nonlocal gate
            gate = asyncio.Event()
            s = new_syncer(tmp, fetch)
            first = asyncio.ensure_future(s.sync_once())
            await asyncio.sleep(0.05)
            s.trigger_sync()
            s.stop()  # 关停清掉待补轮
            gate.set()
            await first
            await asyncio.sleep(0.1)

        asyncio.run(scenario())
        assert state["round"] == 1


def test_digest_change_refetches_and_overwrites():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch1 = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "v1"}
        )
        asyncio.run(new_syncer(tmp, fetch1).sync_once())
        assert (tmp / "a" / "SKILL.md").read_text(encoding="utf-8") == "v1"

        fetch2 = make_fetch(
            [{"id": "10", "name": "a", "version": "2", "digest": "d2"}], {"10": "v2"}
        )
        asyncio.run(new_syncer(tmp, fetch2).sync_once())
        assert (tmp / "a" / "SKILL.md").read_text(encoding="utf-8") == "v2"
        m = read_manifest(tmp)
        assert m["skills"]["a"]["digest"] == "d2"
        assert m["skills"]["a"]["version"] == "2"


def test_collision_names_land_distinct_dirs_and_delete_one_keeps_other():
    # a:b 与 a?b 净化后同为 a_b，靠原名哈希分目录；平台删其一不得伤及另一个。
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [
                {"id": "30", "name": "a:b", "version": "1", "digest": "d1"},
                {"id": "31", "name": "a?b", "version": "1", "digest": "d2"},
            ],
            {"30": "colon", "31": "question"},
        )
        asyncio.run(new_syncer(tmp, fetch).sync_once())
        m = read_manifest(tmp)
        dir_colon, dir_question = m["skills"]["a:b"]["dir"], m["skills"]["a?b"]["dir"]
        assert dir_colon != dir_question
        assert (tmp / dir_colon / "SKILL.md").read_text(encoding="utf-8") == "colon"
        assert (tmp / dir_question / "SKILL.md").read_text(encoding="utf-8") == "question"

        # 平台删掉 a:b → 只清它的目录，a?b 完好。
        fetch2 = make_fetch(
            [{"id": "31", "name": "a?b", "version": "1", "digest": "d2"}], {"31": "question"}
        )
        asyncio.run(new_syncer(tmp, fetch2).sync_once())
        assert not (tmp / dir_colon).exists()
        assert (tmp / dir_question / "SKILL.md").read_text(encoding="utf-8") == "question"
        assert "a:b" not in read_manifest(tmp)["skills"]


def test_dir_rule_migration_cleans_old_dir():
    # 历史 manifest 记的旧目录名与新净化规则产出不同：迁到新目录并清掉旧目录。
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        old_dir = tmp / "a_b"
        old_dir.mkdir(parents=True)
        (old_dir / "SKILL.md").write_text("old", encoding="utf-8")
        (tmp / ".grix-sync.json").write_text(
            json.dumps({"skills": {"a:b": {"id": "30", "version": "1", "digest": "stale", "dir": "a_b"}}}),
            encoding="utf-8",
        )
        fetch = make_fetch(
            [{"id": "30", "name": "a:b", "version": "2", "digest": "d2"}], {"30": "new"}
        )
        asyncio.run(new_syncer(tmp, fetch).sync_once())
        m = read_manifest(tmp)
        new_dir = m["skills"]["a:b"]["dir"]
        assert new_dir != "a_b"
        assert (tmp / new_dir / "SKILL.md").read_text(encoding="utf-8") == "new"
        assert not old_dir.exists()


def test_unchanged_manifest_skips_write_and_on_change():
    """台账无变化：不写盘、不触发 on_change（防每分钟轮询扇出全量上报）。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        hits = []
        writes = []

        async def on_change():
            hits.append(1)

        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "c1"}
        )
        s = new_syncer(tmp, fetch, on_change=on_change)
        orig_write = s._write_manifest

        def spy_write(manifest):
            writes.append(1)
            orig_write(manifest)

        s._write_manifest = spy_write
        asyncio.run(s.sync_once())
        assert hits == [1] and writes == [1]
        # 第二轮清单完全相同：不写盘、不回调。
        asyncio.run(s.sync_once())
        assert hits == [1] and writes == [1]
        # 第三轮元数据真变化（version 升、digest 不变）：写盘 + 回调。
        fetch2 = make_fetch(
            [{"id": "10", "name": "a", "version": "2", "digest": "d1"}], {"10": "c1"}
        )
        s2 = new_syncer(tmp, fetch2, on_change=on_change)
        s2._write_manifest = spy_write
        asyncio.run(s2.sync_once())
        assert hits == [1, 1] and writes == [1, 1]
        assert read_manifest(tmp)["skills"]["a"]["version"] == "2"


def test_digest_hit_backfills_owner_id_and_system():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch1 = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "c1"}
        )
        asyncio.run(new_syncer(tmp, fetch1).sync_once())
        m = read_manifest(tmp)
        assert "owner_id" not in m["skills"]["a"] or m["skills"]["a"].get("owner_id") is None

        fetch2 = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1", "owner_id": "0"}],
            {"10": "c1"},
        )
        asyncio.run(new_syncer(tmp, fetch2).sync_once())
        m2 = read_manifest(tmp)
        assert m2["skills"]["a"]["owner_id"] == "0"
        assert m2["skills"]["a"]["system"] is True
        # digest 命中不再拉 content
        assert all("/content" not in u for u in fetch2.calls[1:])


def test_numeric_owner_id_zero_marks_system():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1", "owner_id": 0}],
            {"10": "c1"},
        )
        asyncio.run(new_syncer(tmp, fetch).sync_once())
        m = read_manifest(tmp)
        assert m["skills"]["a"]["owner_id"] == "0"
        assert m["skills"]["a"]["system"] is True


def test_digest_hit_backfill_then_stable_no_oscillation():
    """存量台账缺 owner_id/system：首轮回填一次，下轮不再判变化（不振荡）。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "a").mkdir()
        (tmp / "a" / "SKILL.md").write_text("c1", encoding="utf-8")
        (tmp / ".grix-sync.json").write_text(
            json.dumps({"skills": {"a": {"id": "10", "version": "1", "digest": "d1", "dir": "a"}}}),
            encoding="utf-8",
        )
        hits = []
        writes = []

        async def on_change():
            hits.append(1)

        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1", "owner_id": "0"}],
            {"10": "c1"},
        )
        s = new_syncer(tmp, fetch, on_change=on_change)
        orig_write = s._write_manifest

        def spy_write(manifest):
            writes.append(1)
            orig_write(manifest)

        s._write_manifest = spy_write
        asyncio.run(s.sync_once())
        m = read_manifest(tmp)
        assert m["skills"]["a"]["owner_id"] == "0"
        assert m["skills"]["a"]["system"] is True
        assert writes == [1] and hits == [1]
        # 第二轮字段已齐：不再写盘、不再回调；digest 命中不拉 content。
        asyncio.run(s.sync_once())
        assert writes == [1] and hits == [1]
        assert all("/content" not in u for u in fetch.calls)


def test_pick_first_reachable_credential():
    """多凭证：首个不可达时取下一个；全部不可达则跳过本轮、不动本地。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        tried = []

        async def fetch(url: str, api_key: str):
            tried.append(api_key)
            if api_key == "bad":
                return None
            if url.endswith("/v1/agent-api/skills"):
                return {"code": 0, "data": {"items": [
                    {"id": "10", "name": "a", "version": "1", "digest": "d1"},
                ]}}
            return {"code": 0, "data": {"content": "c1"}}

        s = SkillSyncer(
            [(ENDPOINT, "bad"), (ENDPOINT, "good")], skills_dir=tmp, fetch_json=fetch
        )
        asyncio.run(s.sync_once())
        assert tried[:2] == ["bad", "good"]
        assert (tmp / "a" / "SKILL.md").read_text(encoding="utf-8") == "c1"

        async def unreachable(url: str, api_key: str):
            return None

        s2 = SkillSyncer(
            [(ENDPOINT, "bad"), (ENDPOINT, "also-bad")], skills_dir=tmp, fetch_json=unreachable
        )
        asyncio.run(s2.sync_once())
        assert (tmp / "a" / "SKILL.md").exists()
        assert read_manifest(tmp)["skills"]["a"]["digest"] == "d1"


def test_update_credentials_takes_effect_next_round():
    """运行期更新凭证（manager 增删 agent）：下一轮用新凭证拉取。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)

        async def unreachable(url: str, api_key: str):
            return None

        s = SkillSyncer([(ENDPOINT, "bad")], skills_dir=tmp, fetch_json=unreachable)
        asyncio.run(s.sync_once())
        assert not (tmp / "a").exists()

        tried = []

        async def fetch(url: str, api_key: str):
            tried.append(api_key)
            if url.endswith("/v1/agent-api/skills"):
                return {"code": 0, "data": {"items": [
                    {"id": "10", "name": "a", "version": "1", "digest": "d1"},
                ]}}
            return {"code": 0, "data": {"content": "c1"}}

        s._fetch_json = fetch
        s.update_credentials([(ENDPOINT, "good")])
        asyncio.run(s.sync_once())
        assert (tmp / "a" / "SKILL.md").exists()
        assert tried and all(k == "good" for k in tried)
        assert read_manifest(tmp)["skills"]["a"]["digest"] == "d1"


def test_cross_owner_manifests_isolated_no_mutual_delete():
    """多 owner 共用库目录：各自独立台账；删目录前校验其它 owner 台账引用。

    复现线上互删场景的防护：owner B 的台账仍引用某目录时，owner A 摘条目不删目录。
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fetch_a = make_fetch(
            [{"id": "10", "name": "shared", "version": "1", "digest": "d1", "owner_id": "1"}],
            {"10": "from-a"},
        )
        fetch_b = make_fetch(
            [{"id": "20", "name": "shared", "version": "1", "digest": "d2", "owner_id": "2"}],
            {"20": "from-b"},
        )
        sa = SkillSyncer(
            [(ENDPOINT, "ka")], skills_dir=tmp, fetch_json=fetch_a,
            manifest_file=".grix-sync-1.json",
        )
        sb = SkillSyncer(
            [(ENDPOINT, "kb")], skills_dir=tmp, fetch_json=fetch_b,
            manifest_file=".grix-sync-2.json",
        )
        asyncio.run(sa.sync_once())
        asyncio.run(sb.sync_once())
        # 各自独立台账；同名技能共享同一目录、后写覆盖（已知限制）。
        ma = json.loads((tmp / ".grix-sync-1.json").read_text(encoding="utf-8"))
        mb = json.loads((tmp / ".grix-sync-2.json").read_text(encoding="utf-8"))
        assert ma["skills"]["shared"]["owner_id"] == "1"
        assert mb["skills"]["shared"]["owner_id"] == "2"
        assert (tmp / "shared" / "SKILL.md").read_text(encoding="utf-8") == "from-b"

        # owner A 平台删掉该技能：A 摘条目，但 B 的台账仍引用该目录 → 目录保留。
        sa2 = SkillSyncer(
            [(ENDPOINT, "ka")], skills_dir=tmp, fetch_json=make_fetch([], {}),
            manifest_file=".grix-sync-1.json",
        )
        asyncio.run(sa2.sync_once())
        ma2 = json.loads((tmp / ".grix-sync-1.json").read_text(encoding="utf-8"))
        assert ma2["skills"] == {}
        assert (tmp / "shared" / "SKILL.md").exists()
        mb2 = json.loads((tmp / ".grix-sync-2.json").read_text(encoding="utf-8"))
        assert "shared" in mb2["skills"]

        # owner B 也删掉后：无任何台账引用，目录才被清掉。
        sb2 = SkillSyncer(
            [(ENDPOINT, "kb")], skills_dir=tmp, fetch_json=make_fetch([], {}),
            manifest_file=".grix-sync-2.json",
        )
        asyncio.run(sb2.sync_once())
        assert not (tmp / "shared").exists()


def test_legacy_manifest_adopted_on_first_start_and_removed():
    """owner syncer 首次 start 收养旧 .grix-sync.json（改名）并删除旧文件。

    旧台账滞留会让平台已删除的技能在合并读取（library/enable/sync-state）
    下永久可见、目录因跨台账删除保护永不清理——删除语义在存量机器失效。
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "x").mkdir()
        (tmp / "x" / "SKILL.md").write_text("c", encoding="utf-8")
        # 旧版台账（升级前写入）引用目录 x；平台侧该技能已删除。
        (tmp / ".grix-sync.json").write_text(
            json.dumps({"skills": {"legacy": {"id": "1", "version": "1", "digest": "d", "dir": "x"}}}),
            encoding="utf-8",
        )

        async def scenario():
            s = SkillSyncer(
                [(ENDPOINT, "k9")], skills_dir=tmp, fetch_json=make_fetch([], {}),
                manifest_file=".grix-sync-9.json",
            )
            await s.start()
            s.stop()

        asyncio.run(scenario())
        # 旧文件已消亡；收养条目并入本 owner 台账后，本轮远端为空 → 摘条目并
        # 清掉不再被任何台账引用的目录。
        assert not (tmp / ".grix-sync.json").exists()
        m9 = json.loads((tmp / ".grix-sync-9.json").read_text(encoding="utf-8"))
        assert m9["skills"] == {}
        assert not (tmp / "x").exists()


def test_adopted_entry_digest_hit_no_refetch():
    """收养的条目 digest 命中远端：直接沿用目录，无需重拉 content。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "a").mkdir()
        (tmp / "a" / "SKILL.md").write_text("c1", encoding="utf-8")
        (tmp / ".grix-sync.json").write_text(
            json.dumps({"skills": {"a": {"id": "10", "version": "1", "digest": "d1", "dir": "a"}}}),
            encoding="utf-8",
        )
        fetch = make_fetch(
            [{"id": "10", "name": "a", "version": "1", "digest": "d1"}], {"10": "c1"}
        )

        async def scenario():
            s = SkillSyncer(
                [(ENDPOINT, "k9")], skills_dir=tmp, fetch_json=fetch,
                manifest_file=".grix-sync-9.json",
            )
            await s.start()
            s.stop()

        asyncio.run(scenario())
        assert not (tmp / ".grix-sync.json").exists()
        m9 = json.loads((tmp / ".grix-sync-9.json").read_text(encoding="utf-8"))
        assert m9["skills"]["a"]["digest"] == "d1"
        # 只拉了清单，没有拉 content。
        assert fetch.calls and all("/content" not in u for u in fetch.calls)
        assert (tmp / "a" / "SKILL.md").read_text(encoding="utf-8") == "c1"


def test_adoption_merge_keeps_own_entry_and_bounds_foreign_cost():
    """本 owner 已有台账时合并收养：旧条目只补缺不覆盖新数据；误收他 owner
    条目的代价有界——摘条目时其目录仍受他 owner 台账引用保护，不会被误删。
    """
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        (tmp / "m").mkdir()
        (tmp / "m" / "SKILL.md").write_text("mine", encoding="utf-8")
        (tmp / "y").mkdir()
        (tmp / "y" / "SKILL.md").write_text("theirs", encoding="utf-8")
        # 旧台账：含与本 owner 同名但 digest 陈旧的条目，以及他 owner 的条目。
        (tmp / ".grix-sync.json").write_text(
            json.dumps({"skills": {
                "mine": {"id": "1", "version": "0", "digest": "stale", "dir": "m"},
                "yours": {"id": "2", "version": "1", "digest": "d2", "dir": "y"},
            }}),
            encoding="utf-8",
        )
        # 本 owner（9）的新台账已有 mine 的最新条目。
        (tmp / ".grix-sync-9.json").write_text(
            json.dumps({"skills": {
                "mine": {"id": "1", "version": "1", "digest": "d1", "dir": "m"},
            }}),
            encoding="utf-8",
        )
        # 他 owner（2）的台账仍引用目录 y。
        (tmp / ".grix-sync-2.json").write_text(
            json.dumps({"skills": {
                "theirs": {"id": "20", "version": "1", "digest": "d2", "dir": "y"},
            }}),
            encoding="utf-8",
        )
        # owner 9 远端只有 mine。
        fetch = make_fetch(
            [{"id": "1", "name": "mine", "version": "1", "digest": "d1"}], {"1": "mine"}
        )

        async def scenario():
            s = SkillSyncer(
                [(ENDPOINT, "k9")], skills_dir=tmp, fetch_json=fetch,
                manifest_file=".grix-sync-9.json",
            )
            await s.start()
            s.stop()

        asyncio.run(scenario())
        assert not (tmp / ".grix-sync.json").exists()
        m9 = json.loads((tmp / ".grix-sync-9.json").read_text(encoding="utf-8"))
        # 同名条目保留新台账数据（旧台账 digest 未覆盖）；误收条目已摘除。
        assert m9["skills"]["mine"]["digest"] == "d1"
        assert "yours" not in m9["skills"]
        # 目录 y 仍被他 owner 台账引用 → 只摘条目不删目录。
        assert (tmp / "y" / "SKILL.md").read_text(encoding="utf-8") == "theirs"


def test_write_manifest_atomic_via_tmp_and_rename():
    """台账原子写：临时文件 + os.replace，无 tmp 残留、glob 只命中正式台账。"""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        s = new_syncer(tmp, make_fetch([], {}))
        with mock.patch(
            "grix_hermes.skill_syncer.os.replace", wraps=os.replace
        ) as replace_spy:
            s._write_manifest({"skills": {"a": {"id": "1", "digest": "d", "dir": "a"}}})
        assert replace_spy.called
        src, dst = replace_spy.call_args[0]
        # 临时文件与正式台账同目录；临时名不以 .json 结尾，不会被扫描命中。
        assert str(src).endswith(".grix-sync.json.tmp")
        assert str(dst).endswith(".grix-sync.json")
        assert [p.name for p in tmp.glob(".grix-sync*")] == [".grix-sync.json"]
        written = json.loads((tmp / ".grix-sync.json").read_text(encoding="utf-8"))
        assert written["skills"]["a"]["digest"] == "d"
