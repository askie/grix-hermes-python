import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from grix_hermes import update_tool  # noqa: E402


def test_run_command_hides_windows_console(monkeypatch):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(update_tool.os, "name", "nt")
    monkeypatch.setattr(update_tool.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(update_tool.subprocess, "run", _fake_run)

    code, stdout, stderr = update_tool._run_command(["hermes", "plugins", "update", "grix-hermes"])

    assert (code, stdout, stderr) == (0, "ok", "")
    assert calls
    assert calls[0][1]["creationflags"] == 0x08000000
