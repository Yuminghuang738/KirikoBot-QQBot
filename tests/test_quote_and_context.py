"""Quote awareness.

The `reply` segment is only `{"type": "reply", "data": {"id": "..."}}` — no
text, no sender — but the code once assumed the quoted content arrived inline,
so `_reply_note` returned "" for every single quote. Quoting the bot's own
reply (the case the feature exists for) did nothing at all. The id now
resolves against bot_messages / group_messages.
"""
from __future__ import annotations

from dataclasses import dataclass

from prompt_builder import resolve_quote


@dataclass
class _Reply:
    """A reply segment as the platform actually sends it."""

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

    def test_inline_content_skips_the_lookup(self):
        """For someone else's message we already have everything we need.

        (For the bot's *own* message we still have to look up who it was said
        to, so that case always hits the lookup.)
        """
        called = []
        resolve_quote(_Reply(text="晚上吃啥", sender_name="小明"), is_own=False,
                      lookup=lambda mid: called.append(mid))
        assert not called

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

