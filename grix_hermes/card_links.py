"""Grix card deep-link generation for conversation, user-profile, and egg-status cards."""

from __future__ import annotations

import urllib.parse
from typing import Dict, Optional


def _clean(value: Optional[str]) -> str:
    return str(value or "").strip()


def _build_link(label: str, url: str) -> str:
    return f"[{_clean(label)}]({url})"


def build_conversation_card(
    session_id: str,
    session_type: str,
    title: str,
    peer_id: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    session_id = _clean(session_id)
    session_type = _clean(session_type)
    title = _clean(title)
    if not session_id or not session_type or not title:
        raise ValueError("conversation card requires session_id, session_type, and title")

    query = urllib.parse.urlencode({
        "session_id": session_id,
        "session_type": session_type,
        "title": title,
    })
    peer_id = _clean(peer_id)
    if peer_id:
        query += f"&peer_id={urllib.parse.quote(peer_id, safe='')}"

    return _build_link(
        _clean(label) or "打开会话",
        f"grix://card/conversation?{query}",
    )


def build_user_profile_card(
    user_id: str,
    nickname: str,
    peer_type: Optional[str] = None,
    avatar_url: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    user_id = _clean(user_id)
    nickname = _clean(nickname)
    if not user_id or not nickname:
        raise ValueError("user profile card requires user_id and nickname")

    params: Dict[str, str] = {
        "user_id": user_id,
        "peer_type": _clean(peer_type) or "2",
        "nickname": nickname,
    }
    avatar_url = _clean(avatar_url)
    if avatar_url:
        params["avatar_url"] = avatar_url

    return _build_link(
        _clean(label) or "查看 Agent 资料",
        f"grix://card/user_profile?{urllib.parse.urlencode(params)}",
    )


def build_egg_status_card(
    install_id: str,
    status: str,
    step: str,
    summary: str,
    target_agent_id: Optional[str] = None,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    label: Optional[str] = None,
) -> str:
    install_id = _clean(install_id)
    status = _clean(status)
    step = _clean(step)
    summary = _clean(summary)
    if not install_id or not status or not step or not summary:
        raise ValueError("egg status card requires install_id, status, step, and summary")

    params: Dict[str, str] = {
        "install_id": install_id,
        "status": status,
        "step": step,
        "summary": summary,
    }
    target_agent_id = _clean(target_agent_id)
    if target_agent_id:
        params["target_agent_id"] = target_agent_id
    error_code = _clean(error_code)
    if error_code:
        params["error_code"] = error_code
    error_message = _clean(error_message)
    if error_message:
        params["error_msg"] = error_message

    return _build_link(
        _clean(label) or "安装状态",
        f"grix://card/egg_install_status?{urllib.parse.urlencode(params)}",
    )


def build_progress_card(
    label: str,
    percent: Optional[int] = None,
    fallback_label: Optional[str] = None,
) -> str:
    """Build a ``grix://card/progress`` deep-link (one-line label + percent).

    ``percent`` is optional: when ``None`` the card renders an *indeterminate*
    progress bar.  Out-of-range values are clamped into 0..100.
    """
    label = _clean(label)
    if not label:
        raise ValueError("progress card requires label")

    params: Dict[str, str] = {"label": label}
    fb = _clean(fallback_label) or f"进度：{label}"
    if percent is not None:
        pct = max(0, min(100, int(percent)))
        params["percent"] = str(pct)
        fb = f"{fb} {pct}%"

    return _build_link(fb, f"grix://card/progress?{urllib.parse.urlencode(params)}")


def dispatch_card_builder(kind: str, params: Dict[str, object]) -> str:
    kind = _clean(kind)
    if kind == "conversation":
        return build_conversation_card(
            session_id=str(params.get("session_id", "")),
            session_type=str(params.get("session_type", "")),
            title=str(params.get("title", "")),
            peer_id=str(params.get("peer_id", "")) or None,
            label=str(params.get("label", "")) or None,
        )
    if kind == "user-profile":
        return build_user_profile_card(
            user_id=str(params.get("user_id", "")),
            nickname=str(params.get("nickname", "")),
            peer_type=str(params.get("peer_type", "")) or None,
            avatar_url=str(params.get("avatar_url", "")) or None,
            label=str(params.get("label", "")) or None,
        )
    if kind == "egg-status":
        return build_egg_status_card(
            install_id=str(params.get("install_id", "")),
            status=str(params.get("status", "")),
            step=str(params.get("step", "")),
            summary=str(params.get("summary", "")),
            target_agent_id=str(params.get("target_agent_id", "")) or None,
            error_code=str(params.get("error_code", "")) or None,
            error_message=str(params.get("error_message", "")) or None,
            label=str(params.get("label", "")) or None,
        )
    if kind == "progress":
        raw_percent = params.get("percent")
        percent = (
            int(raw_percent)
            if raw_percent is not None and str(raw_percent).strip() != ""
            else None
        )
        return build_progress_card(
            label=str(params.get("label", "")),
            percent=percent,
            fallback_label=str(params.get("fallback_label", "")) or None,
        )
    raise ValueError(f"Unsupported card kind: {kind}")
