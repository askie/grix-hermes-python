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
PLUGIN_NAME = "grix-hermes"
PLUGIN_GIT_REPO = "askie/grix-hermes-python"


def resolve_plugin_version() -> str:
    """Resolve the installed grix-hermes version at runtime."""
    try:
        from importlib.metadata import version

        return version(PLUGIN_NAME)
    except Exception:
        pass
    try:
        import tomllib

        toml_path = Path(__file__).resolve().parent.parent / "pyproject.toml"
        with open(toml_path, "rb") as f:
            data = tomllib.load(f)
        v = data.get("project", {}).get("version")
        if v:
            return v
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

def _data_dir() -> Path:
    d = Path.home() / ".hermes" / ".grix-upgrade"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_file() -> Path:
    return _data_dir() / "state.json"


def _pending_file() -> Path:
    return _data_dir() / "pending.json"


def _read_state() -> Dict[str, Any]:
    f = _state_file()
    if not f.exists():
        return {"daily_attempts": {}, "version_attempts": {}}
    try:
        return json.loads(f.read_text())
    except Exception:
        return {"daily_attempts": {}, "version_attempts": {}}


def _write_state(state: Dict[str, Any]) -> None:
    f = _state_file()
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.rename(f)


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _pending_exists() -> bool:
    return _pending_file().exists()


def _write_pending(from_version: str, target_version: str) -> None:
    _pending_file().write_text(json.dumps({
        "from_version": from_version,
        "target_version": target_version,
        "timestamp": time.time(),
    }))


def _read_pending() -> Optional[Dict[str, Any]]:
    f = _pending_file()
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _remove_pending() -> None:
    f = _pending_file()
    try:
        f.unlink(missing_ok=True)
    except Exception:
        pass


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
    ):
        self._endpoint = endpoint
        self._api_key = api_key
        self._channel = channel
        self._is_busy = is_busy
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
        pending = _read_pending()
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
        _remove_pending()

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
        if _pending_exists():
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
            _write_pending(from_ver, target)

            await self._do_upgrade()

            duration_ms = int((time.monotonic() - start_time) * 1000)
            logger.info("[upgrade] plugin update completed, reporting installed")

            await self._report({
                "from_version": from_ver,
                "to_version": target,
                "status": "installed",
                "duration_ms": duration_ms,
            })

            if self._is_busy and self._is_busy():
                logger.info("[upgrade] waiting for active tasks before restart")
                deadline = time.monotonic() + 3600
                while self._is_busy() and time.monotonic() < deadline and not self._stopped:
                    await asyncio.sleep(5)
                if self._stopped:
                    logger.info("[upgrade] aborted: checker stopped during wait")
                    return

            logger.info("[upgrade] restarting process")
            os.kill(os.getpid(), signal.SIGTERM)

        except Exception as exc:
            msg = str(exc)
            logger.error("[upgrade] failed: %s", msg)
            _remove_pending()
            self._record_failure(target)

            duration_ms = int((time.monotonic() - start_time) * 1000)
            await self._report({
                "from_version": from_ver,
                "to_version": target,
                "status": "failed",
                "error_code": "UPGRADE_FAILED",
                "error_msg": msg[:500],
                "duration_ms": duration_ms,
            })

    # ----- rate limiting -----

    def _check_rate_limit(self, target: str) -> bool:
        state = _read_state()

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
        state = _read_state()
        today = _today()
        state["last_failure_at"] = time.time()
        state["last_failure_version"] = target
        da = state.setdefault("daily_attempts", {})
        da[today] = da.get(today, 0) + 1
        va = state.setdefault("version_attempts", {})
        va[target] = va.get(target, 0) + 1
        _write_state(state)

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
        """Run ``hermes plugins update`` to upgrade the plugin."""
        code, _stdout, stderr = await self._run_cmd(["hermes", "plugins", "update", PLUGIN_NAME])
        if code == 0:
            logger.info("[upgrade] hermes plugins update succeeded")
            return

        logger.info("[upgrade] update failed (exit %d), trying install from source", code)
        code2, _stdout2, stderr2 = await self._run_cmd(
            ["hermes", "plugins", "install", PLUGIN_GIT_REPO, "--enable"],
        )
        if code2 == 0:
            logger.info("[upgrade] hermes plugins install succeeded")
            return

        raise RuntimeError(
            f"both update (exit {code}) and install (exit {code2}) failed; "
            f"stderr={stderr2[:500]}"
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
