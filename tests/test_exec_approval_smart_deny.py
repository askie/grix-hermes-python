"""build_exec_approval_message 的 decisions 矩阵回归测试。

覆盖 hermes-agent 0.20.5 起 send_exec_approval 新增的 allow_permanent/
allow_session/smart_denied 三个 flag 如何影响卡片按钮收口，避免安全敏感的
"永久放行"选项在 Smart DENY 场景下被误展示。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grix_hermes.compat import build_exec_approval_message


def test_default_offers_all_three_decisions():
    msg = build_exec_approval_message(
        approval_id="a1", command="rm -rf /tmp/x", description="dangerous",
        raw_approval_data=None,
    )
    assert msg.biz_card["payload"]["allowed_decisions"] == ["allow-once", "allow-always", "deny"]
    assert "rm -rf /tmp/x" in msg.content


def test_smart_denied_hides_allow_always_but_keeps_command_visible():
    msg = build_exec_approval_message(
        approval_id="a2", command="rm -rf /tmp/x", description="dangerous",
        raw_approval_data={"allow_permanent": True, "allow_session": True, "smart_denied": True},
    )
    assert msg.biz_card["payload"]["allowed_decisions"] == ["allow-once", "deny"]
    assert "allow-always" not in msg.biz_card["payload"]["decision_commands"]
    assert "Smart DENY" in msg.content
    assert "rm -rf /tmp/x" in msg.content


def test_allow_permanent_false_hides_allow_always_without_smart_deny():
    msg = build_exec_approval_message(
        approval_id="a3", command="rm -rf /tmp/x", description="dangerous",
        raw_approval_data={"allow_permanent": False, "allow_session": True, "smart_denied": False},
    )
    assert msg.biz_card["payload"]["allowed_decisions"] == ["allow-once", "deny"]
    assert "Smart DENY" not in msg.content
