"""插件安全扫描回归测试。

`hermes plugins update` / `install` 会用 tools.plugin_guard 扫描整棵插件目录，
任何一条 critical finding 都会得到 dangerous 判定：更新时插件被自动禁用，
全新安装直接被拒且 --force 无法覆盖。历史上是测试夹具里的递归删除命令字面量和
文档里的 agent 配置文件名触发的误判，这个测试把误判挡在提交前。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_repo_tree_passes_plugin_security_scan():
    try:
        from tools.plugin_guard import scan_plugin
    except ImportError:
        pytest.skip("hermes-agent tools.plugin_guard not importable")

    result = scan_plugin(REPO_ROOT, source="grix-hermes")
    blocking = [f for f in result.findings if f.severity in ("critical", "high")]
    assert not blocking, "\n".join(
        f"{f.severity} {f.pattern_id} {f.file}:{f.line} {f.match}" for f in blocking
    )
    assert result.verdict != "dangerous"
