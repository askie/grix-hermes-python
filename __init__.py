"""Hermes plugin entrypoint wrapper for the grix-hermes repository layout."""

try:
    # 正常插件加载：本目录作为包被导入，相对导入内层 grix_hermes 包。
    from .grix_hermes import register
except ImportError:
    # 无父包上下文（如 pytest 把 repo 根当独立模块导入）：回退绝对导入，
    # 此时 repo 根已在 sys.path 上，grix_hermes 可直接解析。
    from grix_hermes import register

__all__ = ["register"]
