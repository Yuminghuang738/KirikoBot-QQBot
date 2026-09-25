"""官方平台客户端的纯逻辑测试（不联网）。

重点是三处最容易出错的地方：
1. 官方只接受纯文本 content，段列表要正确翻译（@ 丢弃、图片走上传）
2. 被动回复必须带原消息 id，且 msg_id+msg_seq 组合唯一、上限 5
3. 实测确认：id / member_openid / union_openid 恒等，群事件才有 username
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "KirikoBot"))

from qq_official import (IncomingMessage, MessageBuilder, QQOfficialClient,
                         _first_image, _plain_text)


class TestMessageTranslation:
    """官方没有「段」概念，翻译错了就是发不出或发错。"""

    def test_text_segments_are_joined(self):
        segs = MessageBuilder().text("你好").text("，在吗").build()
        assert _plain_text(segs) == "你好，在吗"

    def test_at_segments_are_dropped(self):
        """官方不允许任意 @ 群成员 —— @ 只能被忽略，不能塞进 content。"""
        segs = MessageBuilder().at("12345").text(" 说话").build()
        assert _plain_text(segs) == "说话"

    def test_the_first_image_is_picked(self):
        segs = MessageBuilder().text("看图").image("/tmp/a.png").build()
        assert _first_image(segs) == "/tmp/a.png"

    def test_no_image_yields_empty(self):
        assert _first_image(MessageBuilder().text("纯文字").build()) == ""

    def test_reply_segment_is_recorded_but_not_text(self):
        segs = MessageBuilder().reply("ROBOT1.0_x").text("回复").build()
        assert _plain_text(segs) == "回复"
        assert any(s["type"] == "reply" for s in segs)


class TestIncomingMessage:
    """事件解析 —— 字段名与实测到的官方 payload 保持一致。"""

    GROUP_AT = {
        "t": "GROUP_AT_MESSAGE_CREATE",
        "d": {
            "id": "ROBOT1.0_Ja5g",
            "content": " 你好",
            "group_openid": "A968B3FFD260C6D9FF38FA671BC543F5",
            "author": {
                "id": "466A7D064E5495F91DE04FA987EACBA3",
                "member_openid": "466A7D064E5495F91DE04FA987EACBA3",
                "union_openid": "466A7D064E5495F91DE04FA987EACBA3",
                "username": "ユーミン",
                "member_role": "owner",
            },
        },
    }

    def test_group_event_parses(self):
        m = IncomingMessage.from_event(self.GROUP_AT)
        assert m.msg_type == "group"
        assert m.group_id == "A968B3FFD260C6D9FF38FA671BC543F5"
        assert m.user_id == "466A7D064E5495F91DE04FA987EACBA3"
        assert m.user_name == "ユーミン"
        assert m.text == "你好", "官方已去掉 @ 前缀，但仍带一个空格"
        assert m.message_id == "ROBOT1.0_Ja5g"
        assert m.is_at_bot is True

    def test_group_events_carry_a_username(self):
        """实测：单聊事件没有 username，群事件有 —— 面板靠它显示名字。"""
        assert IncomingMessage.from_event(self.GROUP_AT).user_name == "ユーミン"

    def test_private_event_has_no_username(self):
        m = IncomingMessage.from_event({
            "t": "C2C_MESSAGE_CREATE",
            "d": {"id": "x", "content": "你好",
                  "author": {"user_openid": "ABC", "union_openid": "ABC"}},
        })
        assert m.msg_type == "private"
        assert m.group_id == ""
        assert m.user_name == "", "单聊事件没有 username，这是实测结论"

    def test_images_are_collected(self):
        m = IncomingMessage.from_event({
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "x", "group_openid": "g", "content": "看图",
                  "author": {"member_openid": "u"},
                  "attachments": [{"content_type": "image/png", "url": "https://x/1.png"},
                                  {"content_type": "voice", "url": "https://x/2.silk"}]},
        })
        assert m.image_urls == ["https://x/1.png"]
        assert m.has_images

    def test_quoted_message_is_extracted(self):
        """官方发送侧「暂未支持」引用，但收得到 —— message_type=103。"""
        m = IncomingMessage.from_event({
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "x", "group_openid": "g", "content": "他说的啥意思",
                  "author": {"member_openid": "u"},
                  "msg_elements": [{"message_type": 103, "content": "原来那句话",
                                    "author": {"username": "小红"}}]},
        })
        assert m.reply is not None
        assert m.reply.text == "原来那句话"
        assert m.reply.sender_name == "小红"


class TestReplySequencing:
    """被动回复的规则：带原消息 id，且 msg_id+msg_seq 组合唯一。"""

    def _client(self):
        c = QQOfficialClient.__new__(QQOfficialClient)
        c._reply_seq = {}
        import threading
        c._sent_lock = threading.Lock()
        c._recent_sent = []
        c._recorder = None
        return c

    def test_sequence_starts_at_one(self):
        assert self._client()._next_seq("m1") == 1

    def test_sequence_increments_per_message(self):
        c = self._client()
        assert [c._next_seq("m1") for _ in range(3)] == [1, 2, 3]

    def test_sequence_is_per_message(self):
        c = self._client()
        c._next_seq("m1")
        assert c._next_seq("m2") == 1

    def test_sequence_caps_at_five(self):
        """官方规定每条消息最多回 5 次，超出就是发不出去。"""
        c = self._client()
        assert [c._next_seq("m1") for _ in range(7)][-1] == 5

    def test_the_bookkeeping_does_not_grow_forever(self):
        c = self._client()
        for i in range(600):
            c._next_seq(f"m{i}")
        assert len(c._reply_seq) <= 501


class TestOwnMessagesAndRecorder:
    def _client(self):
        import threading
        c = QQOfficialClient.__new__(QQOfficialClient)
        c._reply_seq = {}
        c._sent_lock = threading.Lock()
        c._recent_sent = []
        c._recorder = None
        return c

    def test_matches_by_message_id(self):
        c = self._client()
        c._remember("g1", "ROBOT1.0_abc", "你好呀", "u1")
        assert c.is_own_message("ROBOT1.0_abc") is True

    def test_falls_back_to_exact_text(self):
        c = self._client()
        c._remember("g1", "ROBOT1.0_abc", "这是一句足够长的话", "u1")
        assert c.is_own_message(None, "这是一句足够长的话") is True

    def test_short_text_is_not_matched(self):
        """太短的句子不拿来认领，否则会把别人说的话当成自己的。"""
        c = self._client()
        c._remember("g1", "ROBOT1.0_abc", "好的", "u1")
        assert c.is_own_message(None, "好的") is False

    def test_recorder_gets_the_addressee(self):
        c = self._client()
        got = []
        c.set_recorder(lambda *a: got.append(a))
        c._remember("g1", "m", "文本", "u1")
        assert got == [("g1", "m", "文本", "u1")]

    def test_private_sends_do_not_call_the_recorder(self):
        """recorder 是按群记录的；单聊没有群号，别塞空串进去。"""
        c = self._client()
        got = []
        c.set_recorder(lambda *a: got.append(a))
        c._remember("", "m", "文本", "u1")
        assert got == []


class TestNoMemberList:
    def test_official_has_no_member_list(self):
        """实测/文档都没有群成员列表接口 —— 面板要靠发言逐个认识成员。"""
        c = QQOfficialClient.__new__(QQOfficialClient)
        assert c.get_group_member_list("any") == []


class TestTokenKeeper:
    def test_it_caches_until_near_expiry(self):
        from qq_official import _TokenKeeper
        import time as _t

        k = _TokenKeeper("id", "secret")
        calls = []

        def fake_get():
            calls.append(1)
            return "tok"

        k._refresh_locked = fake_get           # type: ignore[method-assign]
        k._token, k._expire_at = "tok", _t.time() + 3600
        assert k.get() == "tok"
        assert not calls, "还没到续期时间不该重新获取"

    def test_it_refreshes_when_close_to_expiry(self):
        from qq_official import _TokenKeeper
        import time as _t

        k = _TokenKeeper("id", "secret")
        k._refresh_locked = lambda: "new"      # type: ignore[method-assign]
        k._token, k._expire_at = "old", _t.time() + 60   # 不足 5 分钟余量
        assert k.get() == "new"
