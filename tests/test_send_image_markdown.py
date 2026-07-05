"""send_image 覆写单元测试。

宿主网关（hermes-agent gateway/platforms/base.py）会把回复里的
``![alt](url)`` 抽出来改走 ``send_image``；基类兜底把图片降级成
「caption\\nurl」纯文本，导致 Grix 端只显示裸链接。GrixAdapter 覆写
``send_image`` 后应还原成 Markdown 图片原样投递。

走 stub 模式（同 test_final_reply_quote.py），不依赖 hermes-agent host。
"""

import asyncio
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _install_stubs() -> None:
    if "tools" not in sys.modules:
        tools_pkg = types.ModuleType("tools")
        reg = types.ModuleType("tools.registry")

        class _Registry:
            def register(self, **kw):
                pass

        reg.registry = _Registry()
        reg.tool_error = lambda msg: f"ERR:{msg}"
        reg.tool_result = lambda obj: f"OK:{obj}"
        tools_pkg.registry = reg
        sys.modules["tools"] = tools_pkg
        sys.modules["tools.registry"] = reg

    if "gateway" not in sys.modules:
        gw = types.ModuleType("gateway")
        gw_cfg = types.ModuleType("gateway.config")

        class _Platform:
            def __init__(self, name):
                self.value = name

            def __eq__(self, other):
                return getattr(other, "value", None) == self.value

            def __hash__(self):
                return hash(self.value)

        gw_cfg.Platform = _Platform
        gw_cfg.PlatformConfig = lambda **kw: SimpleNamespace(**kw)

        gw_session = types.ModuleType("gateway.session")
        gw_session.build_session_key = lambda *a, **kw: "k"

        gw_platforms = types.ModuleType("gateway.platforms")
        gw_platforms_base = types.ModuleType("gateway.platforms.base")
        gw_platforms_base.BasePlatformAdapter = object
        gw_platforms_base.MessageEvent = type("MessageEvent", (), {})
        gw_platforms_base.MessageType = type("MessageType", (), {"TEXT": "text"})
        gw_platforms_base.ProcessingOutcome = type("ProcessingOutcome", (), {"SUCCESS": object()})
        gw_platforms_base.SendResult = type("SendResult", (), {})

        gw_run = types.ModuleType("gateway.run")
        gw_run._gateway_runner_ref = lambda: None

        sys.modules["gateway"] = gw
        sys.modules["gateway.config"] = gw_cfg
        sys.modules["gateway.session"] = gw_session
        sys.modules["gateway.platforms"] = gw_platforms
        sys.modules["gateway.platforms.base"] = gw_platforms_base
        sys.modules["gateway.run"] = gw_run


_install_stubs()

from grix_hermes import adapter as adapter_mod  # noqa: E402


class _SendResult:
    def __init__(self, success=False, message_id=None, error=None, raw_response=None, retryable=False):
        self.success = success
        self.message_id = message_id
        self.error = error
        self.raw_response = raw_response
        self.retryable = retryable


adapter_mod.SendResult = _SendResult


def _make_adapter():
    inst = adapter_mod.GrixAdapter.__new__(adapter_mod.GrixAdapter)
    inst.name = "grix-test"
    inst.sent = []

    async def _fake_send(chat_id, content, reply_to=None, metadata=None, **kw):
        inst.sent.append({
            "chat_id": chat_id,
            "content": content,
            "reply_to": reply_to,
            "metadata": metadata,
        })
        return _SendResult(success=True, message_id=f"m{len(inst.sent)}")

    inst.send = _fake_send
    return inst


def test_send_image_delivers_markdown_image():
    inst = _make_adapter()
    res = asyncio.run(inst.send_image(
        chat_id="chat1",
        image_url="https://qbank.dhf.pub/m/A-Level/q15.png",
        caption="A-Level·EDEXCEL·Physics Q15",
    ))
    assert res.success
    assert inst.sent[0]["content"] == (
        "![A-Level·EDEXCEL·Physics Q15](https://qbank.dhf.pub/m/A-Level/q15.png)"
    )
    assert inst.sent[0]["chat_id"] == "chat1"


def test_send_image_without_caption_uses_empty_alt():
    inst = _make_adapter()
    asyncio.run(inst.send_image(chat_id="c", image_url="https://x/a.png"))
    assert inst.sent[0]["content"] == "![](https://x/a.png)"


def test_send_image_sanitizes_alt_brackets_and_newlines():
    inst = _make_adapter()
    asyncio.run(inst.send_image(
        chat_id="c",
        image_url="https://x/a.png",
        caption="题目[第15题]\n第二行",
    ))
    assert inst.sent[0]["content"] == "![题目(第15题) 第二行](https://x/a.png)"


def test_send_image_passes_reply_to_and_metadata_through():
    inst = _make_adapter()
    meta = {"thread_id": "t1"}
    asyncio.run(inst.send_image(
        chat_id="c",
        image_url="https://x/a.png",
        caption="cap",
        reply_to="msg9",
        metadata=meta,
    ))
    assert inst.sent[0]["reply_to"] == "msg9"
    assert inst.sent[0]["metadata"] is meta
