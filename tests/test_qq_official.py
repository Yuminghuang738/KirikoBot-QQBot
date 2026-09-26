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
                         QuoteInfo, _first_image, _plain_text)


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
        """元素自带 message_type=103 的形状（有些负载这么嵌）。"""
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

    def test_message_level_103_is_a_quote(self):
        """官方文档的形状：103 是**消息级** message_type，正文在 msg_elements。

        以前只检查元素自身的 message_type==103，而元素里通常不带这个字段 ——
        于是真实事件上的引用解析从来没命中过（不报错，只是永远拿不到内容）。
        """
        m = IncomingMessage.from_event({
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "x", "group_openid": "g", "content": " ",
                  "author": {"member_openid": "u"},
                  "message_type": 103,
                  "msg_elements": [{"content": "今天的学习计划已完成",
                                    "author": {"username": "小华"}}],
                  "message_scene": {"source": "default", "ext": [
                      "msg_idx=REFIDX_zzz==",
                      "auth_token=abc",
                      "ref_msg_idx=TMP_1111-2222"]}},
        })
        assert m.reply is not None
        assert m.reply.text == "今天的学习计划已完成"
        assert m.reply.sender_name == "小华"

    def test_ref_msg_idx_alone_marks_a_quote(self):
        """`ref_msg_idx` 是引用场景的标记；即使没有正文也要记下 id，
        交给 find_quoted 去我们自己的记录里反查。"""
        m = IncomingMessage.from_event({
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "x", "group_openid": "g", "content": "他说的啥意思",
                  "author": {"member_openid": "u"},
                  "message_scene": {"ext": ["msg_idx=REFIDX_a==",
                                            "ref_msg_idx=TMP_999"]}},
        })
        assert m.reply is not None
        assert m.reply.message_id == "TMP_999"

    def test_msg_idx_alone_is_not_a_quote(self):
        """**关键反例**：`msg_idx` 是「本条消息自己的索引」，每条消息都有。

        拿它的存在判断引用会把所有消息都当成引用 —— 这是这套字段最容易踩的坑。
        """
        m = IncomingMessage.from_event({
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "x", "group_openid": "g", "content": "你好",
                  "author": {"member_openid": "u"},
                  "message_type": 0,
                  "message_scene": {"ext": ["msg_idx=REFIDX_b==",
                                            "auth_token=xyz"]}},
        })
        assert m.reply is None

    def test_scene_ext_values_may_contain_equals(self):
        """值是 base64-ish 令牌，里面可能还有 '='，只能按第一个 '=' 切。"""
        from qq_official import parse_scene_ext

        parsed = parse_scene_ext({"ext": ["msg_idx=REFIDX_x==", "auth_token=a=b=c"]})
        assert parsed["msg_idx"] == "REFIDX_x=="
        assert parsed["auth_token"] == "a=b=c"
        assert parse_scene_ext(None) == {}
        assert parse_scene_ext({"ext": ["garbage", 42]}) == {}

    def test_real_captured_event_has_no_quote(self):
        """实测抓到的真实事件：只有消息级 message_type=0 + msg_idx/auth_token，
        不该被误判成引用。"""
        m = IncomingMessage.from_event({
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {"id": "ROBOT1.0_x", "group_openid": "A968B3FF",
                  "group_id": "A968B3FF", "content": " 你好", "message_type": 0,
                  "author": {"id": "466A", "member_openid": "466A",
                             "union_openid": "466A", "member_role": "owner",
                             "username": "ユーミン"},
                  "message_scene": {"source": "default", "ext": [
                      "msg_idx=REFIDX_yTz+NP4EOZBSKsITE9PrjA==",
                      "auth_token=CJc-kAGN47_mRoKFDL3WLg"]}},
        })
        assert m.reply is None
        assert m.group_id == "A968B3FF"
        assert m.user_name == "ユーミン"
        assert m.text == "你好"


class TestQuoteRefId:
    """`quote_ref_id` 要同时认官方字段和历史字段。

    这条曾经是真 bug：`_reply_note` / `resolve_quote` 只读 `message_seq`，
    而官方的 `QuoteInfo` 上根本没有这个属性 → AttributeError 被 except 吞掉，
    于是「引用的是机器人**说给别人**的话」永远识别不出来 —— 而这正是引用感知里
    最要紧的那一种。
    """

    def test_prefers_official_message_id(self):
        from prompt_builder import quote_ref_id

        assert quote_ref_id(QuoteInfo(text="x", message_id="ROBOT1.0_a")) == "ROBOT1.0_a"

    def test_falls_back_to_legacy_message_seq(self):
        from prompt_builder import quote_ref_id

        class Legacy:
            message_seq = 75563830

        assert quote_ref_id(Legacy()) == 75563830

    def test_official_quoteinfo_has_no_message_seq_attribute(self):
        """把「它没有 message_seq」钉住，谁再写 reply.message_seq 会在这里想起原因。"""
        assert not hasattr(QuoteInfo(), "message_seq")

    def test_returns_none_when_nothing_usable(self):
        from prompt_builder import quote_ref_id

        assert quote_ref_id(QuoteInfo()) is None
        assert quote_ref_id(object()) is None

    def test_resolve_quote_survives_a_real_quoteinfo(self):
        """端到端回归：真实 QuoteInfo 走一遍 resolve_quote，不能抛 AttributeError。"""
        from prompt_builder import resolve_quote

        seen: list = []

        def lookup(mid):
            seen.append(mid)
            return {"text": "机器人说给小明的话", "user_name": "", "is_own": True,
                    "target_name": "小明"}

        note = resolve_quote(QuoteInfo(message_id="ROBOT1.0_a"), is_own=False,
                             lookup=lookup, current_user="小红")
        assert seen == ["ROBOT1.0_a"], "反查必须真的用官方 message_id 调一次"
        assert "小明" in note

    def test_resolve_quote_with_empty_text_but_an_id_still_looks_up(self):
        """只有索引没有正文时（ref_msg_idx 单独出现）也要去反查。"""
        from prompt_builder import resolve_quote

        def lookup(mid):
            return {"text": "被引用的原话", "user_name": "小红", "is_own": False,
                    "target_name": ""}

        note = resolve_quote(QuoteInfo(message_id="TMP_1"), is_own=False,
                             lookup=lookup)
        assert "被引用的原话" in note


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


class TestPassiveSendIsTheOnlyWayOut:
    """官方平台**只能被动回复**：不带原消息 `msg_id` 的发送一律被拒：

        HTTP 400 {"message":"主动消息失败, 无权限","code":40034105}

    这里锁住的是**线上真实踩过的坑**：自包含工具（塔罗 / 搜索 / 点歌 / 新闻 /
    一言 / 表情包…）以前调 `client.send_group_msg(group_id, ...)` —— 那是 OneBot
    时代的兼容垫片，不带 `msg_id`，于是在官方平台上全部静默 400：工具执行了、
    消息被平台退掉，用户看到的是「机器人不回话」。
    """

    def _client(self):
        c = QQOfficialClient.__new__(QQOfficialClient)
        c._reply_seq = {}
        import threading
        c._sent_lock = threading.Lock()
        c._recent_sent = []
        c._recorder = None
        c.captured = []

        def fake_request(method, path, **kw):
            c.captured.append((method, path, kw.get("json")))
            return {"id": "ROBOT1.0_sent"}

        c._request = fake_request
        return c

    def _msg(self, kind="group"):
        return IncomingMessage(
            msg_type=kind, user_id="U1",
            group_id="G1" if kind == "group" else "",
            message_id="ROBOT1.0_in.y!out", text="来张塔罗牌",
            user_name="小明",
        )

    def test_group_send_carries_the_original_msg_id(self):
        c = self._client()
        assert c.send(self._msg(), MessageBuilder().text("你好").build()) is True
        method, path, payload = c.captured[0]
        assert method == "POST"
        assert path == "/v2/groups/G1/messages"
        assert payload["msg_id"] == "ROBOT1.0_in.y!out", "不带 msg_id 就会被当主动消息拒掉"
        assert payload["msg_seq"] >= 1
        assert payload["content"] == "你好"

    def test_private_send_goes_to_the_user_endpoint(self):
        c = self._client()
        assert c.send(self._msg("private"), MessageBuilder().text("你好").build()) is True
        _, path, payload = c.captured[0]
        assert path == "/v2/users/U1/messages"
        assert payload["msg_id"] == "ROBOT1.0_in.y!out"

    def test_reply_to_carries_it(self):
        c = self._client()
        c.reply_to(self._msg(), "文字回复")
        assert c.captured[0][2]["msg_id"] == "ROBOT1.0_in.y!out"

    def test_reply_image_with_no_text_and_a_failed_upload_sends_nothing(self):
        """图片上传失败又没有文字可退 → 宁可不发，也不发一条空消息。

        （官方对空 content 的文本消息会直接拒绝，发出去只是多一次失败。）
        """
        c = self._client()
        assert c.reply_image(self._msg(), "/nonexistent/nope.png") is False
        assert c.captured == []

    def test_send_text_is_a_reply_not_a_proactive_push(self):
        c = self._client()
        c.send_text(self._msg(), "摘要")
        assert c.captured[0][2]["msg_id"] == "ROBOT1.0_in.y!out"

    def test_the_proactive_shims_are_gone(self):
        """`send_group_msg(group_id, ...)` 在官方平台上不可能成功 —— 它没有
        msg_id。留着它当兼容垫片，只会让所有工具静默失败。"""
        for dead in ("send_group_msg", "send_private_msg"):
            assert not hasattr(QQOfficialClient, dead), (
                f"{dead} 又回来了：官方平台没有主动推送，这条路必定 400(40034105)。"
                "要发消息请用 send(msg, ...) / RobotServer.send(...)")
