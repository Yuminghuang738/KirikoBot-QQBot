"""Quote awareness —— 机器人要认出「被引的是不是我自己说过的、说给谁的」。

这条链路上每一环都曾经是断的，而且**断得都不出声**（没有报错，只是永远
拿不到被引内容），所以这里的测试分两层：零件（`resolve_quote` 的行为）
和端到端（真实官方事件的形状 → 提示文案）。

官方平台给引用信息的方式（依据官方文档 group_at_message_create）：

* 被引正文在 `msg_elements[].content`，作者在 `msg_elements[].author.username`；
* `message_type == 103` 是**消息级**字段，表示「这条消息是引用消息」；
* `message_scene.ext` 里 `ref_msg_idx=` 是引用场景的标记
  （`msg_idx=` 是本条消息自己的索引，**每条都有**）。

事件里**没有**的是「被引那句话当初是说给谁的」—— 那只能拿被引正文/索引回
我们自己的 `bot_messages` 反查，这也正是「B 引用了机器人说给 A 的话」能被
识别出来的原因。
"""
from __future__ import annotations

from dataclasses import dataclass

from prompt_builder import resolve_quote


@dataclass
class _Reply:
    """引用信息的通用桩（同时覆盖官方字段与历史字段）。"""

    message_seq: int | None = None
    sender_id: str = ""
    sender_name: str = ""
    text: str = ""
    has_images: bool = False
    target_name: str = ""   # who the bot said it to; filled in by our lookup


class TestLLBotSendsOnlyAnId:
    """The regression: an id-only segment carries nothing to describe."""

    def test_an_id_only_segment_yields_nothing_without_a_lookup(self):
        assert resolve_quote(_Reply(message_seq=75563830), is_own=False) == ""

    def test_the_lookup_supplies_the_text(self):
        note = resolve_quote(
            _Reply(message_seq=75563830), is_own=False,
            lookup=lambda mid: {"text": "数字音响啊…啥症状？", "user_name": "", "is_own": True},
        )
        assert "数字音响啊" in note

    def test_quoting_the_bot_is_recognised_as_own(self):
        """The whole point: another user quotes a reply the bot gave someone."""
        note = resolve_quote(
            _Reply(message_seq=352360897), is_own=False,
            lookup=lambda mid: {"text": "那你去跟豆包聊啊", "user_name": "", "is_own": True},
        )
        assert "你自己" in note
        assert "不要当成新话题" in note

    def test_quoting_another_member_names_them(self):
        note = resolve_quote(
            _Reply(message_seq=-77706900), is_own=False,
            lookup=lambda mid: {"text": "你没比豆包强哪里去啊", "user_name": "Anonymous",
                                "is_own": False},
        )
        assert "Anonymous" in note
        assert "你自己" not in note

    def test_an_unknown_id_still_yields_nothing(self):
        assert resolve_quote(_Reply(message_seq=1), is_own=False,
                             lookup=lambda mid: None) == ""

    def test_a_broken_lookup_does_not_raise(self):
        def boom(mid):
            raise RuntimeError("db gone")

        assert resolve_quote(_Reply(message_seq=1), is_own=False, lookup=boom) == ""

    def test_inline_content_still_works(self):
        """Some LLBot builds do populate the segment; don't regress that."""
        note = resolve_quote(_Reply(text="晚上吃啥", sender_name="小明"), is_own=False)
        assert "晚上吃啥" in note and "小明" in note

    def test_the_lookup_always_runs_because_it_owns_is_own_and_target(self):
        """有引用就查库 —— 哪怕事件已经给了正文和昵称。

        这里以前断言的是「别人的消息，信息齐了就别查了」（省一次查询）。
        那个优化在官方平台上是**有害**的：`is_own` 和「说给谁的」只有我们自己的
        库知道，跳过查库就永远认不出「被引的是机器人自己说过的话」。
        实测症状：对方引用了机器人对别人说的话，生成出来的说明是
        「引用的是 Kiriko 说过的话」—— 把机器人当成了群里的第三个人。
        """
        called = []
        resolve_quote(_Reply(text="晚上吃啥", sender_name="小明"), is_own=False,
                      lookup=lambda mid: called.append(mid))
        assert called, "查库是拿到 is_own / target_name 的唯一途径，不能被跳过"

    def test_an_own_message_still_looks_up_the_addressee(self):
        called = []
        resolve_quote(_Reply(text="晚上吃啥"), is_own=True,
                      lookup=lambda mid: called.append(mid))
        assert called, "we need to know who it was said to"

    def test_an_image_quote_is_described(self):
        assert "图片" in resolve_quote(_Reply(has_images=True), is_own=False)

    def test_no_reply_segment_at_all(self):
        assert resolve_quote(None, is_own=False) == ""


class TestFindQuoted:
    def test_resolves_a_message_the_bot_sent(self, db):
        db.record_bot_message("g1", 352360897, "那你去跟豆包聊啊")
        found = db.find_quoted("g1", 352360897)
        assert found["is_own"] is True
        assert found["text"] == "那你去跟豆包聊啊"

    def test_resolves_another_members_message(self, db):
        db.record_group_message("g1", "u2", "Anonymous", "你没比豆包强哪里去啊",
                                message_id=-77706900)
        found = db.find_quoted("g1", -77706900)
        assert found["is_own"] is False
        assert found["user_name"] == "Anonymous"

    def test_an_unknown_id_returns_none(self, db):
        assert db.find_quoted("g1", 999999) is None

    def test_a_nonsense_id_returns_none(self, db):
        assert db.find_quoted("g1", None) is None
        assert db.find_quoted("g1", "not-a-number") is None

    def test_the_scoped_lookup_is_tried_first(self, db):
        db.record_bot_message("g1", 123, "g1 的那条")
        assert db.find_quoted("g1", 123)["text"] == "g1 的那条"

    def test_a_group_id_mismatch_still_resolves(self, db):
        """Scoping alone failed silently: a wrong group id dropped the note and
        the bot answered as if nothing had been quoted. QQ ids are unique
        account-wide, so falling back to a global lookup is safe and turns an
        invisible miss into a hit."""
        db.record_bot_message("g1", 123, "给 g1 的")
        found = db.find_quoted("some-other-group", 123)
        assert found is not None and found["text"] == "给 g1 的"

    def test_a_private_chat_lookup_still_works_without_a_group(self, db):
        db.record_group_message("g1", "u1", "小明", "群里说的", message_id=321)
        assert db.find_quoted(None, 321)["text"] == "群里说的"

    def test_the_bots_own_message_wins_over_a_same_id_member_message(self, db):
        """Ids collide across stores; the bot's line is what matters here."""
        db.record_bot_message("g1", 555, "机器人说的")
        db.record_group_message("g1", "u1", "某人", "别人说的", message_id=555)
        found = db.find_quoted("g1", 555)
        assert found["is_own"] is True

    def test_recalled_bot_messages_are_still_resolvable_for_context(self, db):
        """A quote of a recalled line still explains what was being answered."""
        db.record_bot_message("g1", 777, "被撤回的话")
        db.mark_bot_message_recalled(777)
        assert db.find_quoted("g1", 777)["text"] == "被撤回的话"


class TestSpeakerChangeAwareness:
    """B quoting what the bot said to A must not be answered as if B were A.

    The quoted text alone was not enough: the bot had the words but not the
    fact that they were addressed to somebody else, so it recycled A's tone and
    assumptions for B.
    """

    def test_the_addressee_is_recorded_with_the_bots_message(self, db):
        db.record_bot_message("g1", 555, "给你看看这个", target_user_id="2002")
        assert db.fetch_quoted_target("g1", 555) == "2002"

    def test_the_addressee_is_resolved_to_a_name(self, db):
        db.record_group_message("g1", "2002", "小红", "我先问的", message_id=1)
        db.record_bot_message("g1", 555, "给你看看这个", target_user_id="2002")
        assert db.find_quoted("g1", 555)["target_name"] == "小红"

    def test_an_unresolvable_addressee_is_blank_not_wrong(self, db):
        db.record_bot_message("g1", 556, "给谁的呢", target_user_id="9999")
        assert db.find_quoted("g1", 556)["target_name"] == ""

    def test_the_column_is_migrated_onto_old_dbs(self, tmp_path):
        import sqlite3

        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE bot_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT,
                message_id INTEGER, text TEXT DEFAULT '',
                recalled INTEGER DEFAULT 0, ts_exact REAL, created_at DATETIME)"""
        )
        conn.execute("INSERT INTO bot_messages (group_id, message_id, text)"
                     " VALUES ('g1', 1, '老消息')")
        conn.commit()
        conn.close()

        db = DatabaseManager(path)
        cols = {r[1] for r in db.fetch_data("PRAGMA table_info(bot_messages)")}
        assert "target_user_id" in cols
        kept = db.fetch_data("SELECT text FROM bot_messages WHERE message_id=1")
        assert kept[0][0] == "老消息", "migration must not lose rows"

    def test_the_note_says_the_speaker_changed(self):
        from prompt_builder import describe_reply

        note = describe_reply(
            _Reply(message_seq=1, text="给你看看这个", target_name="小红"),
            is_own=True, current_user="小明")
        assert "换了个人" in note
        assert "对「小红」说的话" in note
        assert "现在说话的是「小明」" in note
        assert "不是 小红" in note

    def test_the_note_warns_against_recycling_tone(self):
        from prompt_builder import describe_reply

        note = describe_reply(
            _Reply(message_seq=1, text="x", target_name="小红"),
            is_own=True, current_user="小明")
        assert "别把对方当成 小红" in note
        assert "熟络程度" in note

    def test_the_same_person_quoting_gets_the_plain_note(self):
        from prompt_builder import describe_reply

        note = describe_reply(
            _Reply(message_seq=1, text="给你看看这个", target_name="小明"),
            is_own=True, current_user="小明")
        assert "换了个人" not in note
        assert "就是对这个用户「小明」说的" in note

    def test_an_unknown_addressee_gets_the_plain_note(self):
        from prompt_builder import describe_reply

        note = describe_reply(
            _Reply(message_seq=1, text="x"), is_own=True, current_user="小明")
        assert "换了个人" not in note
        assert "不要当成新话题" in note

    def test_quoting_someone_else_is_unaffected(self):
        from prompt_builder import describe_reply

        note = describe_reply(
            _Reply(message_seq=1, text="x", sender_name="波奇"),
            is_own=False, current_user="小明")
        assert "波奇" in note
        assert "换了个人" not in note

    def test_resolve_quote_passes_the_speaker_through(self):
        from prompt_builder import resolve_quote

        note = resolve_quote(
            _Reply(message_seq=7), is_own=False,
            lookup=lambda mid: {"text": "给你看看这个", "user_name": "",
                                "is_own": True, "target_name": "小红"},
            current_user="小明")
        assert "换了个人" in note



class TestEndToEndOfficialQuote:
    """从**官方事件**一路走到提示文案的端到端回归。

    这条链路之前每一环都是断的，而且断得都不出声：

    1. 事件解析只认 `msg_elements` 里元素自带的 `message_type == 103`，
       而按官方文档 103 是**消息级**字段 → 引用压根没被解出来；
    2. 解出来之后，`_reply_note` / `resolve_quote` 读的是 `reply.message_seq`，
       而 `QuoteInfo` 只有 `message_id` → `AttributeError` 被 `except` 吞掉，
       「拿 id 去我们自己记录里反查」那一步从来没执行过；
    3. 就算走到最后，`resolve_quote` 里的
       `dataclasses.replace(reply, target_name=...)` 也会 `TypeError`，
       因为 `QuoteInfo` 没有 `target_name` 字段。

    所以这里不测零件，直接测那句话：**用户 B 引用了机器人说给 A 的话，
    机器人能不能意识到「说话的人换了」。**
    """

    BOT_MSG_ID = "ROBOT1.0_aaaa.bbbb!cccc"

    def _quote_event(self, quoted_text: str, quoter: str) -> dict:
        """官方文档里「引用消息」的形状：消息级 message_type=103 +
        msg_elements 里的正文 + message_scene.ext 里的 ref_msg_idx。"""
        return {
            "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": "ROBOT1.0_new.msg!id",
                "content": "他说的什么意思",
                "group_openid": "GRP",
                "message_type": 103,
                "author": {"member_openid": "B_OPENID", "username": quoter},
                "msg_elements": [{"content": quoted_text,
                                  "author": {"username": "Kiriko"}}],
                "message_scene": {"ext": ["msg_idx=REFIDX_1==",
                                          "ref_msg_idx=TMP_1"]},
            },
        }

    def test_quoting_what_the_bot_said_to_someone_else_is_recognised(self, db):
        from qq_official import IncomingMessage

        # 机器人之前对「小明」说过一句话：记进我们自己的库（官方字符串 id）
        db.record_bot_message("GRP", self.BOT_MSG_ID, "今晚一起打游戏吗",
                              target_user_id="A_OPENID")
        # 小明的昵称也要有来源，find_quoted 才能把 A_OPENID 还原成人名
        db.record_group_message("GRP", "A_OPENID", "小明", "好啊")

        incoming = IncomingMessage.from_event(self._quote_event("今晚一起打游戏吗", "小红"))
        assert incoming.reply is not None, "第一步就该解出引用"
        # 引用带的索引是 TMP_ 形式，和我们存的 ROBOT1.0_ 消息 id 不是一套编号 ——
        # 所以按 id 查不到，真正的还原靠「事件给的被引正文」去匹配自己的记录。
        assert incoming.reply.message_id == "TMP_1"
        assert db.find_quoted("GRP", "TMP_1") is None, "id 查不到是预期内的"

        # 这里的 lookup 必须和 main._reply_note 的真实接线一致（带上 quoted_text）
        note = resolve_quote(
            incoming.reply,
            is_own=True,                      # 被引的是机器人自己说过的话
            lookup=lambda mid: db.find_quoted("GRP", mid,
                                              quoted_text=incoming.reply.text),
            current_user="小红",
        )
        assert "换了个人" in note, f"没识别出说话的人变了：{note!r}"
        assert "小明" in note
        assert "小红" in note

    def test_quoting_an_unknown_message_does_not_explode(self, db):
        """引用的是一条我们没记录过的消息：安静地退回普通措辞，不能抛异常。"""
        from qq_official import IncomingMessage

        incoming = IncomingMessage.from_event(self._quote_event("谁也没见过的话", "小红"))
        note = resolve_quote(incoming.reply, is_own=False,
                             lookup=lambda mid: db.find_quoted(
                                 "GRP", mid, quoted_text=incoming.reply.text),
                             current_user="小红")
        assert isinstance(note, str)

    def test_short_quoted_text_is_not_used_as_a_key(self, db):
        """正文兜底要有长度护栏，否则一句「好」会到处撞上机器人的话。"""
        db.record_bot_message("GRP", "ROBOT1.0_x", "好", target_user_id="A_OPENID")
        assert db.find_quoted("GRP", "TMP_none", quoted_text="好") is None

    def test_recalled_bot_lines_are_not_used_as_a_key(self, db):
        """撤回过的发言不该再被当成引用目标。"""
        db.record_bot_message("GRP", "ROBOT1.0_y", "这条已经被撤回了",
                              target_user_id="A_OPENID")
        db.mark_bot_message_recalled("ROBOT1.0_y")
        assert db.find_quoted("GRP", "TMP_none",
                              quoted_text="这条已经被撤回了") is None

    def test_official_string_id_round_trips_through_find_quoted(self, db):
        """官方 id 是 ~120 字符的字符串，曾是 int() 强转的受害者 —— 必须原样存取。"""
        db.record_bot_message("GRP", self.BOT_MSG_ID, "记住这句话",
                              target_user_id="A_OPENID")
        found = db.find_quoted("GRP", self.BOT_MSG_ID)
        assert found is not None
        assert found["is_own"] is True
        assert found["text"] == "记住这句话"

    def test_multiline_quoted_text_still_matches(self, db):
        """库里的正文是**原样**存的（含换行、连续空格），而 needle 是归一化过的。

        只做 SQL 相等的话，一条带换行的机器人发言永远匹配不上 —— 引用它时
        就会退化成「引用的是群里某个人说过的话」。
        """
        db.record_bot_message("GRP", "ROBOT1.0_nl", "第一行\n第二行   有   空格",
                              target_user_id="A_OPENID")
        db.record_group_message("GRP", "A_OPENID", "小明", "嗯")
        found = db.find_quoted("GRP", "TMP_none", quoted_text="第一行\n第二行   有   空格")
        assert found is not None
        assert found["is_own"] is True
        assert found["target_name"] == "小明"

    def test_the_text_fallback_only_looks_at_our_own_lines(self, db):
        """群友说过一模一样的话，不能因此被认成「机器人自己说的」。"""
        db.record_group_message("GRP", "A_OPENID", "小明", "这句话是群友说的")
        assert db.find_quoted("GRP", "TMP_none",
                              quoted_text="这句话是群友说的") is None
