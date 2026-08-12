"""进程级自定义技能同步管理器（docs/architecture/38，对齐 connector 用户级设计）。

背景（线上事故）：一台宿主可能跑多个分属不同 owner 的 agent，共用
``~/.grix/skills`` 库目录。旧实现每个 adapter 各起一个 SkillSyncer 轮询同一份
``.grix-sync.json`` 台账：多 owner 互相把对方技能当"平台已删除"清掉，触发全量
content 重拉风暴；且每轮成功都无条件强制全量上报，agent_skills_update 刷屏。

本管理器按 owner_id 分桶：同 owner 的多个 agent 共享一个 SkillSyncer（聚合各
agent 的 (endpoint, api_key) 凭证，syncer 每轮取首个可达者，对齐 connector
pickReachable）；台账按 owner 隔离为 ``.grix-sync-<owner_id>.json``。某 owner
首个 adapter 注册时启动 syncer，最后一个注销时停止并清理。仅台账真变化时才
回调该 owner 所有已注册 adapter 的 on_change（真实变更很罕见，不做单 reporter
去重，不依赖服务端扇入行为）。

假设运行在同一事件循环；register/unregister 的桶登记用 asyncio.Lock 串行化，
syncer.start（含首轮同步）在锁外执行，多 agent 同时重连互不阻塞。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .skill_syncer import Credential, SkillSyncer

logger = logging.getLogger(__name__)

_OWNER_ID_UNSAFE = re.compile(r"[^0-9A-Za-z_-]")

# on_change 回调签名：台账真变化时通知 adapter 强制刷新 skills 上报。
OnChange = Callable[[], Awaitable[None]]

# syncer 工厂（测试注入用）：(credentials, manifest_file, on_change) -> SkillSyncer。
SyncerFactory = Callable[[List[Credential], str, OnChange], SkillSyncer]


def sanitize_owner_id(owner_id: str) -> str:
    """owner_id 净化值（来自服务端 auth_ack，防御性净化）。

    桶 key 与台账文件名统一用它：避免净化碰撞（如 "a/b" 与 "a?b"）时桶按
    原始值分开、台账文件却同名互相覆盖。
    """
    return _OWNER_ID_UNSAFE.sub("_", str(owner_id).strip())


def owner_manifest_file(owner_id: str) -> str:
    """按 owner 隔离的台账文件名。"""
    return f".grix-sync-{sanitize_owner_id(owner_id)}.json"


class _OwnerBucket:
    """单个 owner 的同步状态：已注册 adapter、聚合凭证、共享 syncer。"""

    def __init__(self) -> None:
        self.adapters: Dict[Any, OnChange] = {}
        self.credentials: Dict[Any, Credential] = {}
        self.syncer: Optional[SkillSyncer] = None


class SkillSyncManager:
    """按 owner 分桶的进程级 SkillSyncer 托管单例。"""

    _instance: Optional["SkillSyncManager"] = None

    def __init__(self, *, syncer_factory: Optional[SyncerFactory] = None) -> None:
        self._buckets: Dict[str, _OwnerBucket] = {}
        self._lock = asyncio.Lock()
        self._syncer_factory: SyncerFactory = syncer_factory or _default_syncer_factory

    @classmethod
    def instance(cls) -> "SkillSyncManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def register(
        self,
        adapter: Any,
        *,
        owner_id: str,
        endpoint: str,
        api_key: str,
        on_change: OnChange,
    ) -> None:
        """把 adapter 登记进其 owner 的桶；首个注册启动 syncer。

        以 adapter 实例为 key，幂等：connect() 重入（宿主重连）重复注册只更新
        回调与凭证，不会起第二个 syncer。锁内只做建桶/登记，syncer.start（含
        首轮同步，每凭证 15s 超时）在锁外 await——多 agent 同时重连不被串行化。
        """
        owner_key = sanitize_owner_id(owner_id)
        start_syncer: Optional[SkillSyncer] = None
        async with self._lock:
            bucket = self._buckets.get(owner_key)
            if bucket is None:
                bucket = _OwnerBucket()
                self._buckets[owner_key] = bucket
            bucket.adapters[adapter] = on_change
            bucket.credentials[adapter] = (endpoint, api_key)
            creds = list(bucket.credentials.values())
            if bucket.syncer is None:
                bucket.syncer = self._syncer_factory(
                    creds, owner_manifest_file(owner_key), self._make_dispatch(bucket)
                )
                start_syncer = bucket.syncer
            else:
                bucket.syncer.update_credentials(creds)
        if start_syncer is None:
            return
        await start_syncer.start()
        async with self._lock:
            # start 期间最后一个 adapter 可能已 unregister：桶被整个删掉（或同
            # owner 重新注册换了新桶）。unregister 已停过该 syncer 一次，但 start
            # 尾声才建起轮询 task，这里兜底再停一次（幂等），杜绝泄漏。
            if self._buckets.get(owner_key) is not bucket:
                start_syncer.stop()
                return
            logger.info(
                "[skill-sync] owner=%s syncer started (%d adapter(s))",
                owner_key,
                len(bucket.adapters),
            )

    async def unregister(self, adapter: Any) -> None:
        """注销 adapter；其 owner 桶空时停止 syncer 并清理。"""
        async with self._lock:
            for owner_key, bucket in list(self._buckets.items()):
                if adapter not in bucket.adapters:
                    continue
                bucket.adapters.pop(adapter, None)
                bucket.credentials.pop(adapter, None)
                if bucket.adapters:
                    if bucket.syncer is not None:
                        bucket.syncer.update_credentials(list(bucket.credentials.values()))
                else:
                    if bucket.syncer is not None:
                        bucket.syncer.stop()
                    del self._buckets[owner_key]
                    logger.info("[skill-sync] owner=%s syncer stopped (no adapters)", owner_key)

    def trigger(self, owner_id: str) -> None:
        """skill_sync 下行指令：触发该 owner 立即补一轮（透传 syncer.trigger_sync()）。"""
        bucket = self._buckets.get(sanitize_owner_id(owner_id))
        if bucket is not None and bucket.syncer is not None:
            bucket.syncer.trigger_sync()

    @staticmethod
    def _make_dispatch(bucket: _OwnerBucket) -> OnChange:
        async def _dispatch() -> None:
            # 台账真变化 → 该 owner 所有已注册 adapter 各自强制刷新上报。
            for cb in list(bucket.adapters.values()):
                try:
                    await cb()
                except Exception as exc:
                    logger.debug("[skill-sync] on_change dispatch failed: %s", exc)

        return _dispatch


def _default_syncer_factory(
    credentials: List[Credential], manifest_file: str, on_change: OnChange
) -> SkillSyncer:
    return SkillSyncer(
        credentials=credentials, manifest_file=manifest_file, on_change=on_change
    )
