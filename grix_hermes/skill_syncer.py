"""Custom-skill multi-machine sync for grix-hermes (docs/architecture/38).

Mirrors grix-connector's ``src/core/skill-sync/skill-syncer.ts``: pull the owner's
skill library via the agent-api REST endpoints and land it under
``~/.grix/skills`` (shared with grix-connector). Hermes then enables selected
skills into ``~/.hermes/skills`` via soft links (skill_enable).

Only platform-synced skills (tracked in the manifest) are ever touched; skills
the user created locally are not in the manifest and are never deleted. When the
platform is unreachable the round is skipped — local skills are never wiped on
network failure.

多 owner 宿主（一台机器跑分属不同 owner 的多个 agent、共用 ~/.grix/skills）下，
由 SkillSyncManager 按 owner 分桶各起一个 SkillSyncer，台账按 owner 隔离为
``.grix-sync-<owner_id>.json``，互不摘对方的条目；删除目录前还会校验其它
owner 的台账是否仍引用该目录，杜绝线上出现过的互相误删→全量重拉风暴。

已知限制：两个 owner 存在同名技能时共享同一目录，后写者覆盖前者内容
（目录布局按技能名净化，不按 owner 隔离；消费方按合并台账读取，可接受）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .skill_paths import MANIFEST_FILE, migrate_legacy_hermes_library, resolve_library_skills_dir
from .upgrade_checker import ws_to_http

logger = logging.getLogger(__name__)

# 技能同步以事件驱动为主：平台 skill_sync 下行触发 trigger_sync 立即补拉，
# connect 首轮 sync_once 对齐离线期变更。周期循环只是防丢事件的低频安全网
#（长连接下 Redis 广播万一丢失时的自愈手段），6h 一次、流量可忽略。
DEFAULT_INTERVAL_S = 6 * 3600
REQUEST_TIMEOUT_S = 15

# (endpoint, api_key) 凭证对；一台机器上同 owner 多个 agent 的凭证聚成列表，
# 每轮取首个可达者拉取（对齐 connector pickReachable）。
Credential = Tuple[str, str]

# fetch_json(url, api_key) -> parsed body dict, or None on any failure.
FetchJson = Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]]

_RESERVED_NAMES = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
_ILLEGAL_CHARS = re.compile(r"[<>:\"|?*\x00-\x1f]")


def safe_dir_name(name: str) -> Optional[str]:
    """技能名落盘前的目录名净化（与 connector safeDirName 逐条对齐）。"""
    cleaned = name.strip()
    cleaned = re.sub(r"[/\\]", "_", cleaned)
    cleaned = _ILLEGAL_CHARS.sub("_", cleaned)
    cleaned = re.sub(r"^\.+", "", cleaned)
    cleaned = re.sub(r"[. ]+$", "", cleaned)
    if not cleaned or ".." in cleaned:
        return None
    if _RESERVED_NAMES.match(cleaned):
        return None
    if cleaned != name:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        return f"{cleaned}-{digest}"
    return cleaned


async def _default_fetch_json(url: str, api_key: str) -> Optional[Dict[str, Any]]:
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers={"Authorization": f"Bearer {api_key}"}
            ) as resp:
                if resp.status != 200:
                    logger.warning("[skill-sync] %s returned %d", url, resp.status)
                    return None
                return await resp.json()
    except Exception as exc:
        logger.warning("[skill-sync] %s error: %s", url, exc)
        return None


class SkillSyncer:
    """Pull-based skill syncer: platform library -> ~/.grix/skills."""

    def __init__(
        self,
        credentials: List[Credential],
        skills_dir: Optional[Path] = None,
        interval_s: int = DEFAULT_INTERVAL_S,
        fetch_json: Optional[FetchJson] = None,
        on_change: Optional[Callable[[], Awaitable[None]]] = None,
        manifest_file: str = MANIFEST_FILE,
    ):
        self._credentials = list(credentials)
        self._skills_dir = skills_dir or resolve_library_skills_dir()
        self._interval_s = interval_s
        self._fetch_json = fetch_json or _default_fetch_json
        # 仅当同步台账真变化后回调（对齐 connector manifestChanged 语义）：
        # 相同清单的周期同步不得放大为每个 adapter 一次全量 skills 上报。
        self._on_change = on_change
        # 台账文件名：默认旧版 .grix-sync.json；SkillSyncManager 按 owner 传
        # .grix-sync-<owner_id>.json 实现多 owner 隔离。
        self._manifest_file = manifest_file
        self._running = False
        self._rerun_pending = False
        self._stopped = False
        self._task: Optional[asyncio.Task] = None
        self._migrated = False

    def update_credentials(self, credentials: List[Credential]) -> None:
        """运行期更新凭证列表（manager 增删同 owner agent 时调用）。"""
        self._credentials = list(credentials)

    async def start(self) -> None:
        self._stopped = False
        self._maybe_migrate()
        self._adopt_legacy_manifest()
        try:
            await self.sync_once()
        except Exception as exc:
            logger.warning("[skill-sync] initial sync failed: %s", exc)
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._stopped = True
        self._rerun_pending = False
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    def trigger_sync(self) -> None:
        """主动触发一次同步（平台 skill_sync 变更指令、WS 重连补拉两条路径调用）。"""
        if self._stopped:
            return
        if self._running:
            self._rerun_pending = True
            return
        task = asyncio.ensure_future(self.sync_once())
        task.add_done_callback(_log_task_error)

    def _maybe_migrate(self) -> None:
        if self._migrated:
            return
        self._migrated = True
        # 仅默认库路径时迁移；测试注入的临时 skills_dir 不触碰真实 ~/.hermes。
        if self._skills_dir != resolve_library_skills_dir():
            return
        try:
            migrate_legacy_hermes_library(library_dir=self._skills_dir)
        except Exception as exc:
            logger.warning("[skill-sync] legacy migrate failed: %s", exc)

    def _adopt_legacy_manifest(self) -> None:
        """收养旧版 .grix-sync.json 台账（owner 隔离前的存量），并删除旧文件。

        仅本 syncer 使用 owner 隔离台账（manifest_file 非旧文件名）时生效。
        旧台账若滞留：其引用会让"平台已删除"的技能在合并读取（library/enable/
        sync-state）下永久可见、目录因跨台账删除保护永不清理，且 glob 排序使
        旧台账后读覆盖同名新条目。首个 start 的 owner syncer 把旧台账整体收下：
        本 owner 远端仍在的技能下一轮 digest 命中无需重拉；误收他 owner 的条目
        代价有界——摘条目（其目录仍有他 owner 台账引用保护），至多一次重拉。
        migrate_legacy_hermes_library 迁入库目录的旧文件名台账也经此处被收养。
        """
        if self._manifest_file == MANIFEST_FILE:
            return
        legacy = self._skills_dir / MANIFEST_FILE
        own = self._skills_dir / self._manifest_file
        try:
            if not legacy.exists():
                return
            if not own.exists():
                # 本 owner 尚无台账：直接改名收养。os.replace 原子，多 owner
                # 并发 start 时只有一个改得成，其余拿 FileNotFoundError 跳过。
                os.replace(legacy, own)
                logger.info("[skill-sync] adopted legacy manifest -> %s", own)
                return
            # 本 owner 已有台账：旧条目只补缺（不覆盖新数据），然后删旧文件。
            parsed = json.loads(legacy.read_text(encoding="utf-8"))
            skills = parsed.get("skills") if isinstance(parsed, dict) else None
            if isinstance(skills, dict) and skills:
                manifest = self._read_manifest()
                for name, entry in skills.items():
                    if isinstance(entry, dict):
                        manifest["skills"].setdefault(name, entry)
                self._write_manifest(manifest)
            legacy.unlink()
            logger.info("[skill-sync] merged legacy manifest into %s and removed it", own)
        except Exception as exc:
            logger.warning("[skill-sync] adopt legacy manifest failed: %s", exc)

    async def _loop(self) -> None:
        try:
            while not self._stopped:
                await asyncio.sleep(self._interval_s)
                if self._stopped:
                    return
                try:
                    await self.sync_once()
                except Exception as exc:
                    logger.warning("[skill-sync] sync failed: %s", exc)
        except asyncio.CancelledError:
            pass

    async def sync_once(self) -> None:
        if self._running:
            return  # 防重入：一轮未完不重叠。
        self._running = True
        manifest_changed = False
        try:
            picked = await self._pick_reachable()
            if picked is None:
                return  # 全部凭证不可达 → 跳过本轮，绝不据此删本地。
            endpoint, api_key, remote = picked

            self._skills_dir.mkdir(parents=True, exist_ok=True)
            manifest = self._read_manifest()
            # 台账前后快照比对：无实际变化则不写盘、不回调（防周期同步扇出全量上报）。
            manifest_before = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
            remote_names = {s["name"] for s in remote}

            # 新增/变更：按 digest 判定，只拉变化的技能全文。
            for s in remote:
                local = manifest["skills"].get(s["name"])
                owner_id = s.get("owner_id")
                if owner_id is not None:
                    owner_id = str(owner_id)
                if local and local.get("digest") == s.get("digest"):
                    # digest 命中：仅当 id/version/owner_id/system 真变化才改写条目
                    # （对齐 connector）——否则每轮把缺失字段盖来盖去会让 JSON
                    # 振荡，manifest_changed 恒为 true，扇出全量 agent_skills_update。
                    # digest 相同也必须回填 owner_id/system（升级前存量台账可能缺
                    # 字段），否则平台系统技能（owner_id=0）会长期可被 enable。
                    next_owner = owner_id if owner_id is not None else local.get("owner_id")
                    if next_owner is not None:
                        next_owner = str(next_owner)
                    next_entry = {
                        **local,
                        "id": s["id"],
                        "version": s.get("version", ""),
                        # 兼容 "0" / 0；勿用 `or ""`（数字 0 会被当成假值）。
                        "system": str(next_owner if next_owner is not None else "") == "0",
                    }
                    if next_owner is not None:
                        next_entry["owner_id"] = next_owner
                    else:
                        next_entry.pop("owner_id", None)
                    if (
                        local.get("id") != next_entry.get("id")
                        or local.get("version") != next_entry.get("version")
                        or local.get("owner_id") != next_entry.get("owner_id")
                        or local.get("system") != next_entry.get("system")
                    ):
                        manifest["skills"][s["name"]] = next_entry
                    continue
                system = str(owner_id if owner_id is not None else "") == "0"
                dir_name = safe_dir_name(s["name"])
                if not dir_name:
                    logger.warning("[skill-sync] skip skill with unsafe name: %s", s["name"])
                    continue
                content = await self._fetch_content(endpoint, api_key, s["id"])
                if content is None:
                    continue  # 单条失败不影响其它技能。
                try:
                    skill_dir = self._skills_dir / dir_name
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
                    entry = {
                        "id": s["id"],
                        "version": s.get("version", ""),
                        "digest": s.get("digest", ""),
                        "dir": dir_name,
                        "system": system,
                    }
                    if owner_id is not None:
                        entry["owner_id"] = owner_id
                    manifest["skills"][s["name"]] = entry
                    # 目录规则升级导致 dir 变化：迁走后清掉旧目录，不留孤儿。
                    if (
                        local
                        and local.get("dir")
                        and local["dir"] != dir_name
                        and not self._dir_shared_by_others(manifest, s["name"], local["dir"])
                    ):
                        shutil.rmtree(self._skills_dir / local["dir"], ignore_errors=True)
                except OSError as exc:
                    logger.warning("[skill-sync] write skill %r failed: %s", s["name"], exc)

            # 平台已删除：清掉本地对应技能（仅清本台账记录的；本地自建不在台账，不动）。
            for name in list(manifest["skills"].keys()):
                if name in remote_names:
                    continue
                entry = manifest["skills"][name]
                if entry.get("dir") and not self._dir_shared_by_others(manifest, name, entry["dir"]):
                    shutil.rmtree(self._skills_dir / entry["dir"], ignore_errors=True)
                del manifest["skills"][name]

            if json.dumps(manifest, ensure_ascii=False, sort_keys=True) == manifest_before:
                return
            self._write_manifest(manifest)
            manifest_changed = True
        finally:
            # on_change 放在 running=False 之前（仍在防重入区内）：避免回调扫目录
            # 期间新触发直接起新一轮同步与之交叠。
            if manifest_changed and self._on_change is not None:
                try:
                    await self._on_change()
                except Exception as exc:
                    logger.debug("[skill-sync] on_change callback failed: %s", exc)
            self._running = False
            if self._rerun_pending:
                self._rerun_pending = False
                if not self._stopped:
                    task = asyncio.ensure_future(self.sync_once())
                    task.add_done_callback(_log_task_error)

    # ----- helpers -----

    def _dir_shared_by_others(self, manifest: Dict[str, Any], self_name: str, dir_name: str) -> bool:
        """该目录是否仍被引用（删除/迁移旧目录前的共用校验）。

        除本台账其它条目外，还要扫技能目录下所有其它 owner 的台账（含旧版
        .grix-sync.json）：任何其它台账仍有条目引用该目录就只摘条目不删目录，
        避免多 owner 共用库目录时互相误删触发全量重拉。
        """
        if any(
            n != self_name and e.get("dir") == dir_name
            for n, e in manifest["skills"].items()
        ):
            return True
        for path in self._other_manifest_paths():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            skills = parsed.get("skills") if isinstance(parsed, dict) else None
            if not isinstance(skills, dict):
                continue
            if any(isinstance(e, dict) and e.get("dir") == dir_name for e in skills.values()):
                return True
        return False

    def _other_manifest_paths(self) -> List[Path]:
        try:
            paths = sorted(self._skills_dir.glob(".grix-sync*.json"))
        except Exception:
            return []
        return [p for p in paths if p.name != self._manifest_file]

    async def _pick_reachable(self) -> Optional[Tuple[str, str, List[Dict[str, Any]]]]:
        """依次尝试各凭证，返回首个成功拉到清单的 (endpoint, api_key, 清单)。"""
        for endpoint, api_key in self._credentials:
            if not endpoint or not api_key:
                continue
            remote = await self._fetch_list(endpoint, api_key)
            if remote is not None:
                return endpoint, api_key, remote
        return None

    async def _fetch_list(self, endpoint: str, api_key: str) -> Optional[List[Dict[str, Any]]]:
        base = ws_to_http(endpoint)
        body = await self._fetch_json(f"{base}/v1/agent-api/skills", api_key)
        if not body or body.get("code") != 0:
            return None
        items = (body.get("data") or {}).get("items")
        return items if isinstance(items, list) else []

    async def _fetch_content(self, endpoint: str, api_key: str, skill_id: str) -> Optional[str]:
        base = ws_to_http(endpoint)
        body = await self._fetch_json(
            f"{base}/v1/agent-api/skills/{skill_id}/content", api_key
        )
        if not body or body.get("code") != 0:
            return None
        content = (body.get("data") or {}).get("content")
        return content if isinstance(content, str) else None

    def _read_manifest(self) -> Dict[str, Any]:
        try:
            raw = (self._skills_dir / self._manifest_file).read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("skills"), dict):
                return parsed
        except Exception:
            pass
        return {"skills": {}}

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        """原子写台账：先写同目录临时文件再 rename。

        跨台账删除保护（_dir_shared_by_others）与合并读取会扫这些文件，直接
        write_text 的截断窗口内可能读到半个 JSON 被当成"无引用"而误删目录。
        临时文件名不以 .json 结尾，不会被 .grix-sync*.json 匹配式扫到。
        """
        target = self._skills_dir / self._manifest_file
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)


def _log_task_error(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[skill-sync] triggered sync failed: %s", exc)
