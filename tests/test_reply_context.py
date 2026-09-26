"""Quote-aware context: describing the quoted message.

The platform's `reply` segment carries only an id, so this file covers turning
a resolved quote into a note and never letting the quoted text leak into the
message body (which would make the bot answer its own words as if the user
said them). Parsing the raw OneBot event is covered by test_qq_official.py.
"""
from __future__ import annotations

import sqlite3

from prompt_builder import build_user_message, describe_reply

QUOTED_TEXT = "今天天气不错哦"


class TestDescribeReply:
    class _Reply:
        sender_name = "小王"
        has_images = False

        def __init__(self, text):
            self.text = text

    def test_own_message_is_called_out_explicitly(self):
        note = describe_reply(self._Reply(QUOTED_TEXT), is_own=True)
        assert "你自己" in note
        assert QUOTED_TEXT in note

    def test_other_members_are_named(self):
        note = describe_reply(self._Reply("晚上吃啥"), is_own=False)
        assert "小王" in note and "晚上吃啥" in note

    def test_long_quotes_are_truncated(self):
        note = describe_reply(self._Reply("字" * 500), is_own=False)
        assert len(note) < 400

    def test_image_only_quote_has_a_placeholder(self):
        r = self._Reply("")
        r.has_images = True
        assert "图片" in describe_reply(r, is_own=False)

    def test_none_reply_produces_nothing(self):
        assert describe_reply(None, is_own=False) == ""


class TestUserMessageWithQuote:
    class _Incoming:
        has_images = False
        reply = None

    class _Robot:
        msg_type = "group"
        msg = "那这个呢"
        user_name = "小明"
        group_name = "测试群"
        incoming = None

    def _robot(self):
        r = self._Robot()
        r.incoming = self._Incoming()
        return r

    def test_note_is_placed_next_to_the_message(self):
        text = build_user_message(self._robot(), "【引用回复】什么什么")
        assert text.index("【引用回复】") < text.index("那这个呢")

    def test_without_note_output_is_unchanged(self):
        text = build_user_message(self._robot())
        assert "群「测试群」中" in text
        assert "引用回复" not in text

    def test_the_time_line_leads_the_user_message(self):
        """It lives here, not in the system prompt — see test_prompt_and_frontend."""
        text = build_user_message(self._robot())
        assert text.startswith("当前时间：")
        assert text.index("当前时间：") < text.index("群「测试群」中")


class TestMessageIdStorage:
    def test_records_message_id_and_quote_link(self, db):
        db.record_group_message("g1", "u1", "小明", "那这个呢",
                                message_id=5000, message_seq=4242, reply_to_seq=4242)
        row = db.fetch_data(
            "SELECT message_id, message_seq, reply_to_seq "
            "FROM group_messages WHERE group_id='g1'"
        )[0]
        assert row == ("5000", 4242, 4242)

    def test_message_seq_and_short_id_are_separate_columns(self, db):
        """A reply segment references message_seq, not the platform message_id."""
        db.record_group_message("g1", "u1", "小明", "那这个呢",
                                message_id=987654, message_seq=111)
        row = db.fetch_data(
            "SELECT message_id, message_seq FROM group_messages WHERE group_id='g1'"
        )[0]
        # message_id has TEXT affinity, so a numeric id is stored as its text
        # form; message_seq stays an integer.
        assert row == ("987654", 111)

    def test_columns_are_optional(self, db):
        db.record_group_message("g1", "u1", "小明", "普通消息")
        row = db.fetch_data(
            "SELECT message_id, message_seq, reply_to_seq "
            "FROM group_messages WHERE group_id='g1'"
        )[0]
        assert row == (None, None, None)

    def test_migration_adds_columns_to_an_existing_db(self, tmp_path):
        """An older robot.db has none of the linkage columns."""
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE group_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL, user_id TEXT NOT NULL, user_name TEXT NOT NULL,
                user_role TEXT DEFAULT '', content TEXT NOT NULL,
                msg_type TEXT DEFAULT 'text',
                timestamp DATETIME DEFAULT (datetime('now','localtime'))
            )"""
        )
        conn.execute(
            "INSERT INTO group_messages (group_id,user_id,user_name,content) VALUES ('g1','u1','小明','旧消息')"
        )
        conn.commit()
        conn.close()

        DatabaseManager(path)  # runs the migration

        conn = sqlite3.connect(path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(group_messages)")}
            assert {"message_id", "message_seq", "reply_to_seq"} <= cols
            assert conn.execute("SELECT COUNT(*) FROM group_messages").fetchone()[0] == 1
        finally:
            conn.close()
