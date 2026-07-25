"""Hermes 技能启用主根表（对齐 connector enable-roots.ts 的 hermes 语义）。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class EnableRoots:
    global_root: Optional[Path]
    project_root: Optional[Path]


def resolve_enable_roots(*, home: Optional[Path] = None, cwd: Optional[str] = None) -> EnableRoots:
    """返回 Hermes 的 (global, project) 启用主根。

    - global: ``{home}/.hermes/skills``
    - project: ``{cwd}/.hermes/skills``（无 cwd 时 unavailable，禁止 os.getcwd 兜底）
    """
    home_path = home or Path.home()
    trimmed = (cwd or "").strip()
    return EnableRoots(
        global_root=home_path / ".hermes" / "skills",
        project_root=(Path(trimmed).expanduser() / ".hermes" / "skills") if trimmed else None,
    )
