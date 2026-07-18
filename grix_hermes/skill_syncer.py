"""Custom-skill multi-machine sync for grix-hermes (docs/architecture/38).

Mirrors grix-connector's ``src/core/skill-sync/skill-syncer.ts`` (the reference
implementation): pull the owner's skill library via the agent-api REST endpoints
and land it under ``~/.hermes/skills`` — one copy per machine — so every machine
converges on the platform library. Triggered by the periodic loop and by the
server-pushed ``skill_sync`` packet (beta contract, see backend
``docs/architecture/38`` §6.2).

Only platform-synced skills (tracked in the manifest) are ever touched; skills
the user created locally are not in the manifest and are never deleted. When the
platform is unreachable the round is skipped — local skills are never wiped on
network failure.

Note the landing dir is Hermes' own ``~/.hermes/skills`` (already scanned by
``scan_hermes_skills``), not the connector's ``grix/skills``: on a machine that
also runs grix-connector the two syncers each keep their own copy in disjoint
directories, so they coexist without conflict.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .upgrade_checker import ws_to_http

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 60
MANIFEST_FILE = ".grix-sync.json"
REQUEST_TIMEOUT_S = 15

# fetch_json(url, api_key) -> parsed body dict, or None on any failure.
FetchJson = Callable[[str, str], Awaitable[Optional[Dict[str, Any]]]]

_RESERVED_NAMES = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
_ILLEGAL_CHARS = re.compile(r"[<>:\"|?*\x00-\x1f]")


def safe_dir_name(name: str) -> Optional[str]:
    """技能名落盘前的目录名净化（与 connector safeDirName 逐条对齐）。

    路径穿越、Windows 非法字符（<>:"|?* 与控制符）、结尾点/空格一律替换或剥除；
    保留设备名（con/aux 等）拒绝。净化改变过名字时附加原名短哈希：不同原名可能
    净化出同一目录（a:b 与 a?b 都成 a_b），哈希保证目录与技能一一对应。
    """
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
    """Pull-based skill syncer: platform library -> ~/.hermes/skills."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        skills_dir: Optional[Path] = None,
        interval_s: int = DEFAULT_INTERVAL_S,
        fetch_json: Optional[FetchJson] = None,
        on_change: Optional[Callable[[], Awaitable[None]]] = None,
    ):
        self._endpoint = endpoint
        self._api_key = api_key
        self._skills_dir = skills_dir or (Path.home() / ".hermes" / "skills")
        self._interval_s = interval_s
        self._fetch_json = fetch_json or _default_fetch_json
        # 同步落盘有变化后的回调（如刷新 agent_skills_update 上报），失败不影响同步。
        self._on_change = on_change
        self._running = False
        # 一轮同步进行中又收到触发（如平台连发多条 skill_sync）：记一次待补轮，
        # 本轮结束后补跑，保证不漏掉同步中途发生的变更。（与 connector rerunPending 对齐）
        self._rerun_pending = False
        self._stopped = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._stopped = False
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
        """主动触发一次同步（平台下发 skill_sync 变更指令后调用）。"""
        if self._stopped:
            return
        if self._running:
            self._rerun_pending = True
            return
        task = asyncio.ensure_future(self.sync_once())
        task.add_done_callback(_log_task_error)

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
        changed = False
        try:
            remote = await self._fetch_list()
            if remote is None:
                return  # 平台不可达 → 跳过本轮，绝不据此删本地。

            self._skills_dir.mkdir(parents=True, exist_ok=True)
            manifest = self._read_manifest()
            remote_names = {s["name"] for s in remote}

            # 新增/变更：按 digest 判定，只拉变化的技能全文。
            for s in remote:
                local = manifest["skills"].get(s["name"])
                if local and local.get("digest") == s.get("digest"):
                    continue
                dir_name = safe_dir_name(s["name"])
                if not dir_name:
                    logger.warning("[skill-sync] skip skill with unsafe name: %s", s["name"])
                    continue
                content = await self._fetch_content(s["id"])
                if content is None:
                    continue  # 单条失败不影响其它技能。
                try:
                    skill_dir = self._skills_dir / dir_name
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
                    manifest["skills"][s["name"]] = {
                        "id": s["id"],
                        "version": s.get("version", ""),
                        "digest": s.get("digest", ""),
                        "dir": dir_name,
                    }
                    changed = True
                    # 目录规则升级导致 dir 变化：迁走后清掉旧目录，不留孤儿。
                    if (
                        local
                        and local.get("dir")
                        and local["dir"] != dir_name
                        and not self._dir_shared_by_others(manifest, s["name"], local["dir"])
                    ):
                        shutil.rmtree(self._skills_dir / local["dir"], ignore_errors=True)
                except OSError as exc:
                    # 单条落盘失败（非法残余文件名/权限等）不拖垮整轮。
                    logger.warning("[skill-sync] write skill %r failed: %s", s["name"], exc)

            # 平台已删除：清掉本地对应技能（仅清同步来的；本地自建不在 manifest，不动）。
            for name in list(manifest["skills"].keys()):
                if name in remote_names:
                    continue
                entry = manifest["skills"][name]
                # 目录仍被其它存活技能共用时只摘 manifest，不删目录。
                if entry.get("dir") and not self._dir_shared_by_others(manifest, name, entry["dir"]):
                    shutil.rmtree(self._skills_dir / entry["dir"], ignore_errors=True)
                del manifest["skills"][name]
                changed = True

            self._write_manifest(manifest)
        finally:
            # on_change 放在 running=False 之前（仍在防重入区内）：避免回调扫目录
            # 期间新触发直接起新一轮同步与之交叠（审查建议）。
            if changed and self._on_change is not None:
                try:
                    await self._on_change()
                except Exception as exc:
                    logger.debug("[skill-sync] on_change callback failed: %s", exc)
            self._running = False
            # 本轮期间到达过触发 → 补跑一轮收敛最新状态（已停则不补）。
            if self._rerun_pending:
                self._rerun_pending = False
                if not self._stopped:
                    task = asyncio.ensure_future(self.sync_once())
                    task.add_done_callback(_log_task_error)

    # ----- helpers -----

    @staticmethod
    def _dir_shared_by_others(manifest: Dict[str, Any], self_name: str, dir_name: str) -> bool:
        return any(
            n != self_name and e.get("dir") == dir_name
            for n, e in manifest["skills"].items()
        )

    async def _fetch_list(self) -> Optional[List[Dict[str, Any]]]:
        base = ws_to_http(self._endpoint)
        body = await self._fetch_json(f"{base}/v1/agent-api/skills", self._api_key)
        if not body or body.get("code") != 0:
            return None
        items = (body.get("data") or {}).get("items")
        return items if isinstance(items, list) else []

    async def _fetch_content(self, skill_id: str) -> Optional[str]:
        base = ws_to_http(self._endpoint)
        body = await self._fetch_json(
            f"{base}/v1/agent-api/skills/{skill_id}/content", self._api_key
        )
        if not body or body.get("code") != 0:
            return None
        content = (body.get("data") or {}).get("content")
        # 区分"字段缺失"（异常应答，按单条失败跳过、不落空文件）与"真空内容"。
        return content if isinstance(content, str) else None

    def _read_manifest(self) -> Dict[str, Any]:
        try:
            raw = (self._skills_dir / MANIFEST_FILE).read_text(encoding="utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get("skills"), dict):
                return parsed
        except Exception:
            pass  # 不存在或损坏 → 视为空清单。
        return {"skills": {}}

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        (self._skills_dir / MANIFEST_FILE).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _log_task_error(task: "asyncio.Task[Any]") -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("[skill-sync] triggered sync failed: %s", exc)
