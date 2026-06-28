"""Single source of truth for the grix-hermes version.

The Hermes plugin install spec reads the version from ``plugin.yaml`` (the
plugin manifest), so that file is the one authoritative place the version is
declared. Packaging (pyproject ``dynamic`` version) and the runtime
self-upgrade checker both derive from here, so the version can never drift
between the manifest and the wheel metadata.

Stdlib-only and import-light on purpose: setuptools imports this module at
build time to resolve ``[tool.setuptools.dynamic] version``.
"""

from __future__ import annotations

import re
from pathlib import Path

_VERSION_RE = re.compile(r"""^version:\s*["']?([^"'\s]+)""", re.MULTILINE)


def read_version() -> str:
    """Parse the ``version`` field out of the sibling ``plugin.yaml``."""
    manifest = Path(__file__).resolve().parent / "plugin.yaml"
    try:
        match = _VERSION_RE.search(manifest.read_text(encoding="utf-8"))
        if match:
            return match.group(1)
    except OSError:
        pass
    return "0.0.0"


__version__ = read_version()
