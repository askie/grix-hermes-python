"""Self-upgrade checker for grix-hermes plugin.

Mirrors the upgrade-checker in grix-connector (TypeScript). On a periodic
schedule and on ``connector_upgrade_push`` local-action, queries the backend
upgrade API and, when a newer version is available, performs the upgrade via
``hermes plugins update`` then restarts the process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 6 * 60 * 60  # 6 hours
INITIAL_DELAY_S = 5 * 60  # 5 minutes
COOLDOWN_S = 30 * 60  # 30 minutes after failure
MAX_DAILY_ATTEMPTS = 2
MAX_VERSION_ATTEMPTS = 3
API_TIMEOUT_S = 10
UPDATE_MAX_ATTEMPTS = 4
UPDATE_RETRY_BASE_S = 1.5
PLUGIN_NAME = "grix-hermes"
PLUGIN_GIT_REPO = "askie/grix-hermes-python"


def _hidden_window_subprocess_kwargs() -> Dict[str, Any]:
    if os.name != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _running_plugin_dir_name() -> str:
    """Name of the directory the running plugin code was loaded from.

    Hermes' plugin loader discovers **every** subdirectory of ``plugins/`` that
    contains a ``plugin.yaml`` and lets later (sorted) entries override earlier
    ones on name collision. A stray manual backup such as
    ``grix-hermes.bak.vX.Y.Z/`` therefore *shadows* the real ``grix-hermes/``
    directory: the gateway silently runs the stale backup code while
    ``hermes plugins update`` keeps rewriting the real directory. When this
    happens the running dir name differs from :data:`PLUGIN_NAME` and any
    self-upgrade attempt is futile — the new code is never the code that loads.

    Edge case (accepted): a plain pip/wheel install reports the site-packages
    parent dir, which also differs from :data:`PLUGIN_NAME`. Harmless there —
    self-upgrade only works under a Hermes plugin install anyway, so refusing
    to upgrade remains the right behaviour; only the log wording assumes a
    ``plugins/`` layout.
    """
    return Path(__file__).resolve().parent.parent.name


def _is_shadowed_copy() -> bool:
    """True when the running code does not live in the canonical plugin dir."""
    return _running_plugin_dir_name() != PLUGIN_NAME


def resolve_plugin_version() -> str:
    """Resolve the running grix-hermes version at runtime.

    Reads the running plugin directory's own ``plugin.yaml`` first — that is the
    single authoritative version declaration (see ``_version.py``) and the exact
    file ``hermes plugins update`` (git pull) rewrites on upgrade, so the version
    we report always matches the code actually installed. Package metadata is
    only a fallback for installs where ``plugin.yaml`` is not alongside the code
    (e.g. a pip/wheel install), and must not shadow the directory plugin — a
    stale editable/wheel dist would otherwise make a successful upgrade look
    rolled back.
    """
    try:
        import re

        manifest = Path(__file__).resolve().parent.parent / "plugin.yaml"
        match = re.search(
            r"""^version:\s*["']?([^"'\s]+)""",
            manifest.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    except Exception:
        pass
    try:
        from importlib.metadata import version

        return version(PLUGIN_NAME)
    except Exception:
        pass
    return "0.0.0"


def ws_to_http(ws_url: str) -> str:
    """Extract HTTP origin from a WS URL (protocol + host, no path/query)."""
    url = ws_url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    parsed = urlparse(url)
    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


# ---------------------------------------------------------------------------
#  State / pending file helpers
# ---------------------------------------------------------------------------

def _sanitize_key(agent_id: Optional[object]) -> str:
    # agent_id may arrive as int (snowflake) or str — coerce before sanitizing.
    raw = str(agent_id).strip() if agent_id else ""
    key = "".join(c for c in raw if c.isalnum() or c in "-_")
    return key or "default"


def _data_dir(agent_id: Optional[str] = None) -> Path:
    # Per-agent subdir: multiple gateways (profiles) on one host must never
    # clobber each other's pending/state — a sibling's failed upgrade must not
    # remove our pending file (losing our success report) nor pollute our
    # rate-limit counters.
    d = Path.home() / ".hermes" / ".grix-upgrade" / _sanitize_key(agent_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_file(agent_id: Optional[str] = None) -> Path:
    return _data_dir(agent_id) / "state.json"


def _pending_file(agent_id: Optional[str] = None) -> Path:
    return _data_dir(agent_id) / "pending.json"


def _read_state(agent_id: Optional[str] = None) -> Dict[str, Any]:
    f = _state_file(agent_id)
    if not f.exists():
        return {"daily_attempts": {}, "version_attempts": {}}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {"daily_attempts": {}, "version_attempts": {}}


def _write_state(state: Dict[str, Any], agent_id: Optional[str] = None) -> None:
    f = _state_file(agent_id)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.rename(f)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _pending_exists(agent_id: Optional[str] = None) -> bool:
    return _pending_file(agent_id).exists()


def _write_pending(from_version: str, target_version: str, agent_id: Optional[str] = None) -> None:
    _pending_file(agent_id).write_text(json.dumps({
        "from_version": from_version,
        "target_version": target_version,
        "timestamp": time.time(),
    }))


def _read_pending(agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    f = _pending_file(agent_id)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _remove_pending(agent_id: Optional[str] = None) -> None:
    f = _pending_file(agent_id)
    try:
        f.unlink(missing_ok=True)
    except Exception:
        pass


def _cli_lacks_subcommand(stdout: str, stderr: str, name: str) -> bool:
    """argparse 对未知子命令的报错形态：``invalid choice: 'show'``。"""
    text = f"{stdout or ''}\n{stderr or ''}".lower()
    return "invalid choice" in text and f"'{name}'" in text


def _plugin_enabled_in_config(hermes_home: Optional[str] = None) -> bool:
    """Read ``plugins.enabled`` from config.yaml (the field the incident lost)."""
    home = Path(hermes_home or os.environ.get("HERMES_HOME", "") or "~/.hermes").expanduser()
    try:
        import yaml

        with open(home / "config.yaml", "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001 - unreadable config == unverifiable
        logger.warning("[upgrade] cannot read %s for enable verification: %s", home / "config.yaml", exc)
        return False
    plugins = data.get("plugins") if isinstance(data, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if isinstance(enabled, (list, tuple, set)):
        return PLUGIN_NAME in {str(item).strip() for item in enabled}
    return False


class PluginNotEnabledError(RuntimeError):
    """The plugin update/install succeeded but the plugin is still disabled.

    Distinguished from a plain update/install failure so backend reports
    (and operators reading them) can tell the two failure modes apart.
    """


# ---------------------------------------------------------------------------
#  UpgradeChecker
# ---------------------------------------------------------------------------

class UpgradeChecker:
    """Periodically checks the backend for newer releases and self-upgrades."""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        channel: str = "stable",
        is_busy: Optional[Callable[[], bool]] = None,
        agent_id: Optional[str] = None,
        restart: Optional[Callable[[], bool]] = None,
    ):
        self._endpoint = endpoint
        self._api_key = api_key
        self._channel = channel
        self._is_busy = is_busy
        self._agent_id = agent_id
        self._restart = restart
        self._running = False
        self._stopped = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self._handle_pending_on_startup()
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

    def trigger_check(self) -> None:
        """Trigger an immediate check (called on connector_upgrade_push)."""
        if self._stopped:
            return
        asyncio.ensure_future(self._run_check())

    # ----- lifecycle -----

    async def _loop(self) -> None:
        try:
            await asyncio.sleep(INITIAL_DELAY_S)
            if self._stopped:
                return
            await self._run_check()
            while not self._stopped:
                await asyncio.sleep(CHECK_INTERVAL_S)
                if not self._stopped:
                    await self._run_check()
        except asyncio.CancelledError:
            pass

    async def _handle_pending_on_startup(self) -> None:
        pending = _read_pending(self._agent_id)
        if not pending:
            return

        current = resolve_plugin_version()
        target = pending.get("target_version", "")
        from_ver = pending.get("from_version", "")

        if current == target:
            logger.info("[upgrade] startup version matches target %s, success", target)
            await self._report({
                "from_version": from_ver,
                "to_version": target,
                "status": "success",
            })
        else:
            # 升级「成功」重启后版本仍不符 = 本次升级实际没生效，必须计入失败
            # 限流。否则每轮 check 都会重复 update→restart→mismatch，形成
            # 无限重启循环（成功路径原本从不记失败，限流完全失效）。
            # 计失败写本地 state 文件，I/O 异常不得中断 gateway 启动流程。
            try:
                self._record_failure(target)
            except Exception as exc:
                logger.warning("[upgrade] failed to record startup mismatch: %s", exc)
            if _is_shadowed_copy():
                # 运行代码来自 plugins/ 下的散兵备份目录（如 grix-hermes.bak.vX/
                # —— Hermes 加载器按序发现所有含 plugin.yaml 的子目录，同名后
                # 者覆盖前者），plugins update 更新的正规目录永远不会被加载。
                # 再升级再重启也没用，唯一出路是人工清掉备份目录。
                logger.error(
                    "[upgrade] startup version %s != target %s: plugin is shadowed by "
                    "stray copy %r under plugins/ — remove the backup directory so the "
                    "real %s/ plugin gets loaded; skipping further upgrade attempts",
                    current, target, _running_plugin_dir_name(), PLUGIN_NAME,
                )
                await self._report({
                    "from_version": from_ver,
                    "to_version": target,
                    "status": "failed",
                    "error_code": "PLUGIN_SHADOWED",
                    "error_msg": (
                        f"loaded from stray dir {_running_plugin_dir_name()!r} "
                        f"instead of {PLUGIN_NAME}/ (expected {target}, got {current})"
                    ),
                })
            else:
                logger.warning(
                    "[upgrade] startup version %s != target %s, rolled back",
                    current, target,
                )
                await self._report({
                    "from_version": from_ver,
                    "to_version": target,
                    "status": "rolled_back",
                    "error_code": "STARTUP_MISMATCH",
                    "error_msg": f"expected {target}, got {current}",
                })
        _remove_pending(self._agent_id)

    # ----- check / upgrade -----

    async def _run_check(self) -> None:
        if self._running:
            return
        self._running = True
        try:
            await self._check()
        except Exception as exc:
            logger.error("[upgrade] check failed: %s", exc)
        finally:
            self._running = False

    async def _check(self) -> None:
        if _pending_exists(self._agent_id):
            return

        if _is_shadowed_copy():
            # 运行的是遮蔽副本：plugins update 写的正规目录不会被加载，升级
            # 注定无效，重启只会把 gateway 拖进无限重启循环。直接放弃，等人
            # 工清理备份目录（每次周期检查重报一次，便于告警发现）。
            logger.error(
                "[upgrade] refusing to self-upgrade: running code is shadowed by stray "
                "dir %r under plugins/ (expected %s/) — remove the backup directory",
                _running_plugin_dir_name(), PLUGIN_NAME,
            )
            return

        resp = await self._query_upgrade()
        if not resp.get("available") or not resp.get("release"):
            return

        release = resp["release"]
        target = release["version"]
        from_ver = resolve_plugin_version()
        force = release.get("force", False)

        if not force and not self._check_rate_limit(target):
            return

        logger.info("[upgrade] starting %s -> %s", from_ver, target)
        start_time = time.monotonic()

        try:
            _write_pending(from_ver, target, self._agent_id)

            await self._do_upgrade()

            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.info("[upgrade] plugin update completed, reporting installed")

            await self._report({
                "from_version": from_ver,
                "to_version": target,
                "status": "installed",
                "duration_ms": duration_ms,
            })

            # 等待活跃任务完成——有任务在跑就一直等，清空才重启，绝不强制打断
            if not await self._wait_until_idle():
                logger.info("[upgrade] aborted: checker stopped during wait")
                return

            logger.info("[upgrade] restarting process")
            self._restart_process()

        except Exception as exc:
            msg = str(exc)
            logger.error("[upgrade] failed: %s", msg)
            _remove_pending(self._agent_id)
            self._record_failure(target)

            duration_ms = int((time.monotonic() - start_time) * 1000)
            error_code = "ENABLE_VERIFY_FAILED" if isinstance(exc, PluginNotEnabledError) else "UPGRADE_FAILED"
            await self._report({
                "from_version": from_ver,
                "to_version": target,
                "status": "failed",
                "error_code": error_code,
                "error_msg": msg[:500],
                "duration_ms": duration_ms,
            })

    async def _wait_until_idle(self) -> bool:
        """Wait until all in-flight agent runs finish; True = clear to restart.

        No max-wait: a busy gateway is never force-restarted — the installed
        version simply takes effect at the next natural idle moment. Returns
        False only when the checker is stopped mid-wait (shutdown in
        progress), in which case the caller must skip the restart.
        """
        if not (self._is_busy and self._is_busy()):
            return not self._stopped
        logger.info("[upgrade] waiting until idle before restart (no max wait)")
        last_log = time.monotonic()
        while self._is_busy() and not self._stopped:
            await asyncio.sleep(5)
            if time.monotonic() - last_log >= 600:
                last_log = time.monotonic()
                logger.info("[upgrade] still waiting for active tasks before restart")
        if self._stopped:
            return False
        logger.info("[upgrade] all tasks completed, proceeding with restart")
        return True

    def _restart_process(self) -> None:
        """Restart the hosting process after a successful upgrade.

        Prefers the injected graceful-restart callback: the gateway drains
        in-flight tasks, saves session state, and spawns its own successor,
        so no external supervisor is needed. A bare SIGTERM is only the last
        resort — on Windows it maps to TerminateProcess (hard kill, shutdown
        handlers never run) and it assumes something external revives us.
        """
        if self._stopped:
            # The checker was stopped, i.e. the gateway is already shutting
            # down on its own — injecting a restart (or SIGTERM) here would
            # flip an operator's planned stop into an unwanted revival.
            logger.info("[upgrade] skip restart: shutdown already in progress")
            return
        if self._restart:
            try:
                if self._restart():
                    return
                logger.warning("[upgrade] graceful restart unavailable, falling back to SIGTERM")
            except Exception as exc:
                logger.error("[upgrade] graceful restart failed, falling back to SIGTERM: %s", exc)
        os.kill(os.getpid(), signal.SIGTERM)

    # ----- rate limiting -----

    def _check_rate_limit(self, target: str) -> bool:
        state = _read_state(self._agent_id)

        last_fail = state.get("last_failure_at")
        if last_fail:
            elapsed = time.time() - last_fail
            if elapsed < COOLDOWN_S:
                logger.info("[upgrade] in cooldown, %.0f min remaining", (COOLDOWN_S - elapsed) / 60)
                return False

        va = state.get("version_attempts", {}).get(target, 0)
        if va >= MAX_VERSION_ATTEMPTS:
            logger.info("[upgrade] version %s tried %d times, skip", target, va)
            return False

        da = state.get("daily_attempts", {}).get(_today(), 0)
        if da >= MAX_DAILY_ATTEMPTS:
            logger.info("[upgrade] %d attempts today, skip", da)
            return False

        return True

    def _record_failure(self, target: str) -> None:
        state = _read_state(self._agent_id)
        today = _today()
        state["last_failure_at"] = time.time()
        state["last_failure_version"] = target
        da = state.setdefault("daily_attempts", {})
        da[today] = da.get(today, 0) + 1
        va = state.setdefault("version_attempts", {})
        va[target] = va.get(target, 0) + 1
        _write_state(state, self._agent_id)

    # ----- HTTP API -----

    async def _query_upgrade(self) -> Dict[str, Any]:
        import aiohttp

        base = ws_to_http(self._endpoint)
        url = f"{base}/v1/agent-api/upgrade/check"
        params = {
            "client_type": PLUGIN_NAME,
            "client_version": resolve_plugin_version(),
            "channel": self._channel,
            "platform": platform.system().lower(),
            "arch": platform.machine(),
        }

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    params=params,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                ) as resp:
                    if resp.status != 200:
                        logger.warning("[upgrade] check API returned %d", resp.status)
                        return {"available": False}
                    body = await resp.json()
                    if body.get("code") != 0:
                        return {"available": False}
                    return body.get("data", {"available": False})
        except Exception as exc:
            logger.warning("[upgrade] check API error: %s", exc)
            return {"available": False}

    async def _report(self, report: Dict[str, Any]) -> None:
        import aiohttp

        body: Dict[str, Any] = {
            "client_type": PLUGIN_NAME,
            **report,
            "platform": platform.system().lower(),
            "arch": platform.machine(),
            "node_version": f"python-{platform.python_version()}",
        }
        try:
            body["host_name"] = socket.gethostname()
        except Exception:
            pass
        try:
            import uuid
            body["install_id"] = str(uuid.getnode())
        except Exception:
            pass

        base = ws_to_http(self._endpoint)
        url = f"{base}/v1/agent-api/upgrade/report"

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await session.post(
                    url,
                    json=body,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                )
        except Exception:
            pass  # best effort

    # ----- upgrade execution -----

    async def _do_upgrade(self) -> None:
        """Run ``hermes plugins update`` to upgrade the plugin.

        Retries with randomized backoff: when several gateways (profiles) on one
        host upgrade at once they contend on Hermes' plugin registry lock, and a
        loser exits non-zero even though its own git pull already landed — a
        short retry rides over the contention before the full-reinstall fallback.
        """
        import random

        last_code = -1
        last_out = ""
        for attempt in range(1, UPDATE_MAX_ATTEMPTS + 1):
            code, stdout, stderr = await self._run_cmd(["hermes", "plugins", "update", PLUGIN_NAME])
            if code == 0:
                logger.info("[upgrade] hermes plugins update succeeded (attempt %d)", attempt)
                await self._ensure_enabled()
                return
            # ``hermes`` prints git failures (e.g. dirty tree, non-ff) to stdout, not
            # stderr, so capture both — reporting stderr alone leaves the real cause blank.
            last_code, last_out = code, "\n".join(p for p in (stdout, stderr) if p)
            logger.info(
                "[upgrade] update attempt %d/%d failed (exit %d): %s",
                attempt, UPDATE_MAX_ATTEMPTS, code, last_out[:500] or "<no output>",
            )
            if attempt < UPDATE_MAX_ATTEMPTS:
                await asyncio.sleep(UPDATE_RETRY_BASE_S * attempt + random.uniform(0, 1.0))

        logger.info("[upgrade] update exhausted %d attempts, trying install from source", UPDATE_MAX_ATTEMPTS)
        code2, stdout2, stderr2 = await self._run_cmd(
            ["hermes", "plugins", "install", PLUGIN_GIT_REPO, "--enable"],
        )
        if code2 == 0:
            logger.info("[upgrade] hermes plugins install succeeded")
            await self._ensure_enabled()
            return

        install_out = "\n".join(p for p in (stdout2, stderr2) if p)
        raise RuntimeError(
            f"both update (exit {last_code}) and install (exit {code2}) failed; "
            f"output={(install_out or last_out)[:500] or '<no output>'}"
        )

    async def _ensure_enabled(self) -> None:
        """Guard against ``hermes plugins update`` landing the plugin disabled.

        Observed in production: a successful ``hermes plugins update`` can still
        leave grix-hermes out of ``plugins.enabled`` in config.yaml. Restarting
        into that state silently drops all messaging connectivity (the gateway
        logs "No messaging platforms enabled" with no error anywhere) — so this
        must run, and be verified, before the caller proceeds to restart.
        Raising here aborts ``_do_upgrade`` before the restart, so ``_check``'s
        except-block reports the upgrade as failed and the old, still-connected
        process keeps running instead of restarting into a broken state.
        """
        await self._run_cmd(["hermes", "plugins", "enable", PLUGIN_NAME])
        code, stdout, stderr = await self._run_cmd(["hermes", "plugins", "show", PLUGIN_NAME])
        if code != 0 and _cli_lacks_subcommand(stdout, stderr, "show"):
            # 旧版 Hermes CLI 没有 ``plugins show``：直接读 config.yaml 的
            # plugins.enabled（事故里丢的就是这个字段），而不是把"命令不存在"
            # 当成"未启用"——否则这类主机每次升级都会在校验处失败。
            if _plugin_enabled_in_config():
                logger.info("[upgrade] 'plugins show' unavailable; verified plugins.enabled via config.yaml")
                return
            raise PluginNotEnabledError(
                "plugin not enabled after update; 'hermes plugins show' unavailable "
                "and grix-hermes missing from plugins.enabled in config.yaml"
            )
        if code != 0 or "status: enabled" not in (stdout or "").lower():
            raise PluginNotEnabledError(
                f"plugin not enabled after update; "
                f"status check: {(stdout or stderr)[:300] or '<no output>'}"
            )

    @staticmethod
    async def _run_cmd(
        cmd: List[str],
        timeout: int = 120,
    ) -> Tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_hidden_window_subprocess_kwargs(),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                proc.returncode or 0,
                (stdout_b or b"").decode(errors="replace").strip(),
                (stderr_b or b"").decode(errors="replace").strip(),
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"command timed out after {timeout}s: {cmd}")
