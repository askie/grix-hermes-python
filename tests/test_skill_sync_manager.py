"""SkillSyncManager（进程级按 owner 分桶的技能同步，docs/architecture/38）单元测试。

覆盖：同 owner 多 adapter 共享一个 syncer 且聚合凭证、重复注册幂等、异 owner
各自独立台账、on_change 按 owner 扇出、unregister 至零停 syncer、trigger 透传。
"""

import asyncio
import sys
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes.skill_sync_manager import SkillSyncManager, owner_manifest_file  # noqa: E402


class FakeSyncer:
    """记录行为的假 SkillSyncer（避免真实轮询/网络）。"""

    def __init__(self, credentials, manifest_file, on_change):
        self.credentials = list(credentials)
        self.manifest_file = manifest_file
        self.on_change = on_change
        self.started = 0
        self.stopped = 0
        self.triggers = 0
        self.cred_updates: List[List[Tuple[str, str]]] = []

    async def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def trigger_sync(self):
        self.triggers += 1

    def update_credentials(self, credentials):
        self.credentials = list(credentials)
        self.cred_updates.append(list(credentials))


def make_manager():
    created: List[FakeSyncer] = []

    def factory(credentials, manifest_file, on_change):
        syncer = FakeSyncer(credentials, manifest_file, on_change)
        created.append(syncer)
        return syncer

    return SkillSyncManager(syncer_factory=factory), created


def noop_on_change():
    async def _cb():
        return None

    return _cb


def test_same_owner_adapters_share_one_syncer_with_aggregated_credentials():
    async def run():
        manager, created = make_manager()
        a1, a2 = object(), object()
        await manager.register(
            a1, owner_id="42", endpoint="ws://h/1", api_key="k1", on_change=noop_on_change()
        )
        await manager.register(
            a2, owner_id="42", endpoint="ws://h/2", api_key="k2", on_change=noop_on_change()
        )
        assert len(created) == 1
        syncer = created[0]
        assert syncer.started == 1
        assert syncer.manifest_file == ".grix-sync-42.json"
        assert syncer.credentials == [("ws://h/1", "k1"), ("ws://h/2", "k2")]

    asyncio.run(run())


def test_register_idempotent_on_reconnect():
    """connect() 重入（宿主重连）重复注册同一 adapter：不得起第二个 syncer。"""
    async def run():
        manager, created = make_manager()
        a1 = object()
        await manager.register(
            a1, owner_id="42", endpoint="ws://h/1", api_key="k1", on_change=noop_on_change()
        )
        await manager.register(
            a1, owner_id="42", endpoint="ws://h/1", api_key="k1", on_change=noop_on_change()
        )
        assert len(created) == 1
        assert created[0].started == 1
        # 第二次注册只刷新凭证，不重启。
        assert created[0].cred_updates == [[("ws://h/1", "k1")]]

    asyncio.run(run())


def test_distinct_owners_get_independent_syncers_and_manifests():
    async def run():
        manager, created = make_manager()
        await manager.register(
            object(), owner_id="1", endpoint="ws://h/1", api_key="k1", on_change=noop_on_change()
        )
        await manager.register(
            object(), owner_id="2", endpoint="ws://h/2", api_key="k2", on_change=noop_on_change()
        )
        assert len(created) == 2
        assert {s.manifest_file for s in created} == {
            ".grix-sync-1.json",
            ".grix-sync-2.json",
        }

    asyncio.run(run())


def test_on_change_dispatch_fans_out_to_all_adapters_of_that_owner():
    async def run():
        manager, created = make_manager()
        hits1, hits2, hits3 = [], [], []

        async def cb1():
            hits1.append(1)

        async def cb2():
            hits2.append(1)

        async def cb3():
            hits3.append(1)

        await manager.register(object(), owner_id="1", endpoint="ws://h/1", api_key="k1", on_change=cb1)
        await manager.register(object(), owner_id="1", endpoint="ws://h/2", api_key="k2", on_change=cb2)
        await manager.register(object(), owner_id="2", endpoint="ws://h/3", api_key="k3", on_change=cb3)
        # owner=1 的台账真变化 → 其两个 adapter 都收到强制刷新；owner=2 不受影响。
        await created[0].on_change()
        assert hits1 == [1] and hits2 == [1] and hits3 == []
        await created[1].on_change()
        assert hits3 == [1]

    asyncio.run(run())


def test_unregister_down_to_zero_stops_syncer():
    async def run():
        manager, created = make_manager()
        a1, a2 = object(), object()
        await manager.register(a1, owner_id="42", endpoint="ws://h/1", api_key="k1", on_change=noop_on_change())
        await manager.register(a2, owner_id="42", endpoint="ws://h/2", api_key="k2", on_change=noop_on_change())
        syncer = created[0]

        # 注销一个：syncer 保留，凭证收敛到剩余 adapter。
        await manager.unregister(a1)
        assert syncer.stopped == 0
        assert syncer.credentials == [("ws://h/2", "k2")]

        # 注销到最后一个：syncer 停止并清理桶。
        await manager.unregister(a2)
        assert syncer.stopped == 1
        assert manager._buckets == {}

        # 未注册过的 adapter 注销是 no-op。
        await manager.unregister(object())

    asyncio.run(run())


def test_trigger_forwards_to_owner_syncer():
    async def run():
        manager, created = make_manager()
        await manager.register(object(), owner_id="1", endpoint="ws://h/1", api_key="k1", on_change=noop_on_change())
        await manager.register(object(), owner_id="2", endpoint="ws://h/2", api_key="k2", on_change=noop_on_change())
        manager.trigger("1")
        assert created[0].triggers == 1
        assert created[1].triggers == 0
        # 未注册的 owner / 非法值：静默跳过。
        manager.trigger("999")
        manager.trigger("")

    asyncio.run(run())


def test_owner_manifest_file_sanitizes_unsafe_chars():
    assert owner_manifest_file("42") == ".grix-sync-42.json"
    assert "/" not in owner_manifest_file("../evil")
    assert ".." not in owner_manifest_file("../evil")
