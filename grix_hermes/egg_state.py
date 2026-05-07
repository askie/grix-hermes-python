"""Grix egg bootstrap state management.

Handles checkpoint/resume via JSON state files for the 7-step agent
incubation flow: detect → install → create → bind → soul → gateway → accept.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

STEP_NAMES = ("detect", "install", "create", "bind", "soul", "gateway", "accept")
StepName = str
StepStatus = str  # "pending" | "done" | "failed" | "skipped"
CreatePath = str  # "host" | "http" | "existing" | ""
InteractionStatus = str  # "full" | "degraded" | "none"
DeliveryTargetSource = str
DeliveryMessageKind = str


@dataclass
class StepState:
    status: StepStatus = "pending"
    at: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class DeliveryAttempt:
    kind: str = ""
    at: str = ""
    ok: bool = False
    target: str = ""
    target_source: str = ""
    message_preview: str = ""
    ack: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


@dataclass
class DeliveryState:
    target: str = ""
    target_source: str = "none"
    attempts: List[DeliveryAttempt] = field(default_factory=list)


@dataclass
class StateFile:
    version: int = 2
    install_id: str = ""
    agent_name: str = ""
    profile_name: str = ""
    route: str = ""
    path: CreatePath = ""
    started_at: str = ""
    updated_at: str = ""
    completed_at: Optional[str] = None
    interaction_status: InteractionStatus = "none"
    delivery: DeliveryState = field(default_factory=DeliveryState)
    steps: Dict[StepName, StepState] = field(default_factory=dict)

    def __post_init__(self):
        for name in STEP_NAMES:
            if name not in self.steps:
                self.steps[name] = StepState()


def iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _ensure_private_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private_file(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _redact(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    result = {}
    for k, v in obj.items():
        norm = k.lower().replace("-", "").replace("_", "")
        if norm == "apikey" or norm.endswith("apikey"):
            result[k] = "ak_***" if str(v or "").strip() else ""
        else:
            result[k] = _redact(v)
    return result


def state_file_path(hermes_home: str, install_id: str) -> str:
    return os.path.join(hermes_home, "tmp", f"grix-egg-{install_id}.json")


def make_fresh_state(
    install_id: str,
    agent_name: str,
    profile_name: str = "",
    route: str = "create_new",
) -> StateFile:
    now = iso_now()
    return StateFile(
        install_id=install_id,
        agent_name=agent_name,
        profile_name=profile_name or agent_name,
        route=route,
        started_at=now,
        updated_at=now,
    )


def save_state(path: str, state: StateFile) -> None:
    _ensure_private_dir(os.path.dirname(path))
    state.updated_at = iso_now()
    data = {
        "version": state.version,
        "install_id": state.install_id,
        "agent_name": state.agent_name,
        "profile_name": state.profile_name,
        "route": state.route,
        "path": state.path,
        "started_at": state.started_at,
        "updated_at": state.updated_at,
        "completed_at": state.completed_at,
        "interaction_status": state.interaction_status,
        "delivery": {
            "target": state.delivery.target,
            "target_source": state.delivery.target_source,
            "attempts": [
                {
                    "kind": a.kind,
                    "at": a.at,
                    "ok": a.ok,
                    "target": a.target,
                    "target_source": a.target_source,
                    "message_preview": a.message_preview,
                    "ack": a.ack,
                    "error": a.error,
                }
                for a in state.delivery.attempts
            ],
        },
        "steps": {
            name: {
                "status": s.status,
                "at": s.at,
                "result": s.result,
                "error": s.error,
            }
            for name, s in state.steps.items()
        },
    }
    _write_private_file(path, json.dumps(_redact(data), indent=2, ensure_ascii=False))


def load_state(path: str) -> Optional[StateFile]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    steps = {}
    raw_steps = raw.get("steps") or {}
    if isinstance(raw_steps, dict):
        for name in STEP_NAMES:
            s = raw_steps.get(name) or {}
            steps[name] = StepState(
                status=s.get("status", "pending") if s.get("status") in ("done", "failed", "skipped") else "pending",
                at=s.get("at"),
                result=s.get("result"),
                error=s.get("error"),
            )

    raw_delivery = raw.get("delivery") or {}
    attempts = []
    for a in raw_delivery.get("attempts") or []:
        if isinstance(a, dict):
            attempts.append(DeliveryAttempt(
                kind=str(a.get("kind", "")),
                at=str(a.get("at", "")),
                ok=bool(a.get("ok")),
                target=str(a.get("target", "")),
                target_source=str(a.get("target_source", "")),
                message_preview=str(a.get("message_preview", "")),
                ack=a.get("ack"),
                error=a.get("error"),
            ))

    return StateFile(
        version=int(raw.get("version", 2)),
        install_id=str(raw.get("install_id", "")),
        agent_name=str(raw.get("agent_name", "")),
        profile_name=str(raw.get("profile_name", "")),
        route=str(raw.get("route", "")),
        path=str(raw.get("path", "")),
        started_at=str(raw.get("started_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        completed_at=raw.get("completed_at"),
        interaction_status=str(raw.get("interaction_status", "none")),
        delivery=DeliveryState(
            target=str(raw_delivery.get("target", "")),
            target_source=str(raw_delivery.get("target_source", "none")),
            attempts=attempts,
        ),
        steps=steps,
    )


def mark_step_done(state: StateFile, step: str, result: Dict[str, Any]) -> None:
    state.steps[step] = StepState(status="done", at=iso_now(), result=result)


def mark_step_failed(state: StateFile, step: str, error: str) -> None:
    state.steps[step] = StepState(status="failed", at=iso_now(), error=error)


def mark_step_skipped(state: StateFile, step: str) -> None:
    state.steps[step] = StepState(status="skipped", at=iso_now())


def step_is_done(state: StateFile, step: str) -> bool:
    return state.steps.get(step, StepState()).status == "done"


def refresh_interaction(state: StateFile) -> None:
    if not state.delivery.target:
        state.interaction_status = "none"
        return
    if any(not a.ok for a in state.delivery.attempts):
        state.interaction_status = "degraded"
        return
    success_kinds = {"final_text", "final_card"}
    has_final = all(
        any(a.kind == k and a.ok for a in state.delivery.attempts)
        for k in success_kinds
    )
    failure_kinds = {"failure_text", "failure_card"}
    has_failure = all(
        any(a.kind == k and a.ok for a in state.delivery.attempts)
        for k in failure_kinds
    )
    state.interaction_status = "full" if (has_final or has_failure) else "none"


def record_delivery(
    state: StateFile,
    target: str,
    target_source: str,
    kind: str,
    message: str,
    ok: bool,
    ack: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
) -> None:
    state.delivery.attempts.append(DeliveryAttempt(
        kind=kind,
        at=iso_now(),
        ok=ok,
        target=target,
        target_source=target_source,
        message_preview=message[:160],
        ack=ack,
        error=error,
    ))
    refresh_interaction(state)
