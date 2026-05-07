"""Boolean parsing helpers for tool parameter normalization."""

from __future__ import annotations

from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on", "y"}
_FALSE_VALUES = {"0", "false", "no", "off", "n"}


def to_bool(value: Any, *, default: bool) -> bool:
    """Parse common bool-like values with an explicit fallback."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return default

    text = str(value).strip().lower()
    if not text:
        return default
    if text in _TRUE_VALUES:
        return True
    if text in _FALSE_VALUES:
        return False
    return default
