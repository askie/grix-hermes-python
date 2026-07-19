"""工具栏一键上传（docs/architecture/39 §4）：把本机某个非托管技能的全文上载到平台库。

Mirrors grix-connector's ``src/core/skill-sync/skill-upload.ts``. 上传后平台会广播
skill_sync，本机既有的 SkillSyncer 会照常拉取并把该技能记入台账，此处不重复维护。
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional

from .exec_command import SkillEntry, scan_hermes_skills
from .upgrade_checker import ws_to_http

REQUEST_TIMEOUT_S = 15

# post_json(url, api_key, body) -> parsed response dict, or raises on transport/HTTP error.
# 与 skill_syncer.py 的 fetch_json 注入方式对齐，测试用假实现替换，不必真连网络。
PostJson = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class SkillUploadError(Exception):
    pass


def find_uploadable_skill(name: str, skills: Optional[List[SkillEntry]] = None) -> SkillEntry:
    """在当前扫描结果里按名找到一个可上传的技能（非系统托管）。"""
    target = (name or "").strip()
    if not target:
        raise SkillUploadError("skill name is required")

    entries = skills if skills is not None else scan_hermes_skills()
    hit = next((s for s in entries if s.name == target), None)
    if hit is None:
        raise SkillUploadError(f'skill "{target}" not found')
    if hit.managed:
        raise SkillUploadError(f'skill "{target}" is system-managed and cannot be uploaded')
    if hit.file_path is None:
        raise SkillUploadError(f'skill "{target}" has no local file')
    return hit


async def _default_post_json(url: str, api_key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        ) as resp:
            if resp.status != 200:
                raise SkillUploadError(f"upload rejected: HTTP {resp.status}")
            return await resp.json(content_type=None)


async def upload_skill(
    name: str,
    api_key: str,
    ws_url: str,
    post_json: Optional[PostJson] = None,
) -> None:
    """读取技能全文并 POST 到平台 upload 接口，成功后平台已入库。"""
    skill = find_uploadable_skill(name)
    try:
        content = skill.file_path.read_text(encoding="utf-8")  # type: ignore[union-attr]
    except Exception as exc:
        raise SkillUploadError(f"failed to read skill file: {exc}") from exc

    url = f"{ws_to_http(ws_url)}/v1/agent-api/skills/upload"
    impl = post_json or _default_post_json
    try:
        body = await impl(url, api_key, {"name": skill.name, "content": content})
    except SkillUploadError:
        raise
    except Exception as exc:
        raise SkillUploadError(f"upload request failed: {exc}") from exc

    if not isinstance(body, dict) or body.get("code") != 0:
        msg = body.get("msg") if isinstance(body, dict) else None
        raise SkillUploadError(f"upload rejected: {msg or 'unknown error'}")
