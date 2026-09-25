"""传输层接口的契约测试。

适配器最容易出的问题是「漏实现某个方法」或「能力表跟实现对不上」——
那类错误在运行时才炸，而且往往只在下班后的某条消息上炸。
所以这里用真实调用点反推出的方法清单，逐个卡住。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "KirikoBot"
sys.path.insert(0, str(APP_DIR))

from transport.base import Capabilities, Transport  # noqa: E402

# 全项目对传输层的真实调用（从调用点聚合得来），少一个都会在运行时报错。
REQUIRED_METHODS = (
    "send_group_msg", "send_private_msg", "reply_to", "reply_image", "send_text",
    "get_group_info", "get_group_member_list",
    "recall", "is_own_message", "set_recorder",
    "send_ai_voice", "get_ai_characters",
)


class TestCapabilities:
    def test_onebot_supports_everything(self):
        """OneBot/NapCat 是功能最全的一边，能力表全 True 才是对的。"""
        cap = Capabilities()
        assert cap.proactive and cap.group_context and cap.voice
        assert cap.mention_member and cap.recall and cap.quote_reply
        assert cap.local_image_path
        assert cap.reply_window_seconds is None, "OneBot 没有 5 分钟窗口"

    def test_capabilities_are_frozen(self):
        """能力表是适配器的静态声明，业务代码不该改它。"""
        with pytest.raises(Exception):
            Capabilities().proactive = False  # type: ignore[misc]


class TestOneBotTransport:
    def _transport(self):
        from transport.onebot import OneBotTransport

        return OneBotTransport.__new__(OneBotTransport)

    def test_declares_a_name(self):
        assert self._transport().name == "onebot"

    def test_implements_every_required_method(self):
        t = self._transport()
        missing = [m for m in REQUIRED_METHODS if not callable(getattr(t, m, None))]
        assert not missing, f"Transport 少了这些方法: {missing}"

    def test_satisfies_the_protocol(self):
        """runtime_checkable 的 Protocol 检查：属性齐了才算合格。"""
        t = self._transport()
        assert isinstance(t, Transport)

    def test_the_protocol_lists_exactly_what_is_used(self):
        """接口方法集合要和真实调用点一致 —— 多了是负担，少了会炸。"""
        for m in REQUIRED_METHODS:
            assert hasattr(Transport, m), f"Transport 未声明 {m}"

    def test_it_delegates_instead_of_reimplementing(self):
        """过渡层必须纯委托；混进业务逻辑就失去了可替换性。"""
        import inspect

        from transport.onebot import OneBotTransport

        src = inspect.getsource(OneBotTransport)
        assert "self._client." in src
        # 不该出现任何平台判断或业务分支。用词边界，免得 verify/modify
        # 这类词被当成 if 误伤。
        body = src.split('"""', 2)[-1]      # 去掉模块 docstring
        assert not re.search(r"\b(if|elif|else|try|except)\b", body), \
            "过渡层里出现了分支语句，应该是纯委托"
        assert "getattr(" not in body
