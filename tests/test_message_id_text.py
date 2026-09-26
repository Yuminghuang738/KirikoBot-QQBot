"""`message_id` is a string on the official platform.

Official message ids look like ``ROBOT1.0_xxxxxxxx.yyyy!zzzz`` — ~120 chars,
with ``.`` and ``!`` — not integers. The old schema declared ``message_id`` as
INTEGER and the read path did ``int(message_id)``, whose ValueError was
swallowed into "not found": quote awareness silently stopped working instead of
raising. These tests pin the column type, the round-trip for both the new and
the legacy numeric id space, and the PRAGMA-probe table rebuild that upgrades
an existing INTEGER database.
"""
from __future__ import annotations

import sqlite3

OFFICIAL_ID = "ROBOT1.0_abcdEFGH.1234!ijklMNOP"


class TestColumnTypes:
    def _types(self, db, table: str) -> dict[str, str]:
        return {r[1]: (r[2] or "").upper() for r in db.fetch_data(f"PRAGMA table_info({table})")}

    def test_group_messages_message_id_is_text(self, db):
        assert self._types(db, "group_messages")["message_id"] == "TEXT"

    def test_bot_messages_message_id_is_text(self, db):
        assert self._types(db, "bot_messages")["message_id"] == "TEXT"


class TestOfficialIdRoundTrip:
    def test_a_group_message_is_resolved(self, db):
        db.record_group_message("g1", "u1", "小明", "官方 id 的消息",
                                message_id=OFFICIAL_ID)
        found = db.find_quoted("g1", OFFICIAL_ID)
        assert found is not None
        assert found["text"] == "官方 id 的消息"
        assert found["is_own"] is False

    def test_the_bots_own_message_is_recognised(self, db):
        db.record_bot_message("g1", OFFICIAL_ID, "机器人说的话", target_user_id="2002")
        found = db.find_quoted("g1", OFFICIAL_ID)
        assert found is not None
        assert found["is_own"] is True
        assert found["text"] == "机器人说的话"

    def test_the_addressee_is_returned(self, db):
        db.record_bot_message("g1", OFFICIAL_ID, "给你看看", target_user_id="2002")
        assert db.fetch_quoted_target("g1", OFFICIAL_ID) == "2002"

    def test_marking_recalled_by_official_id(self, db):
        db.record_bot_message("g1", OFFICIAL_ID, "撤回我")
        db.mark_bot_message_recalled(OFFICIAL_ID)
        assert db.get_last_bot_message("g1") is None


class TestLegacyNumericIdsStillWork:
    def test_a_numeric_id_query_matches_a_stored_id(self, db):
        """Old rows hold numeric ids; TEXT affinity must not break lookups."""
        db.record_bot_message("g1", "75563830", "老数字 id")
        found = db.find_quoted("g1", 75563830)
        assert found is not None
        assert found["text"] == "老数字 id"
        assert db.fetch_quoted_target("g1", 75563830) == ""

    def test_a_legacy_numeric_insert_round_trips(self, db):
        db.record_group_message("g1", "u1", "小明", "数字 id", message_id=4242)
        assert db.find_quoted("g1", 4242)["text"] == "数字 id"


class TestEmptyValuesAreSafe:
    def test_find_quoted_rejects_none_and_empty(self, db):
        assert db.find_quoted("g1", None) is None
        assert db.find_quoted("g1", "") is None

    def test_fetch_quoted_target_rejects_none_and_empty(self, db):
        assert db.fetch_quoted_target("g1", None) == ""
        assert db.fetch_quoted_target("g1", "") == ""

    def test_an_unknown_official_id_is_none(self, db):
        assert db.find_quoted("g1", "ROBOT1.0_nope.nope!nope") is None


class TestLegacyIntegerDatabaseIsRebuilt:
    """SQLite cannot ALTER a column type, so old INTEGER tables are rebuilt."""

    @staticmethod
    def _make_legacy_db(path: str) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE group_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL, user_id TEXT NOT NULL,
                user_name TEXT NOT NULL, user_role TEXT DEFAULT '',
                content TEXT NOT NULL, msg_type TEXT DEFAULT 'text',
                timestamp DATETIME DEFAULT (datetime('now','localtime')),
                message_id INTEGER, message_seq INTEGER,
                reply_to_seq INTEGER, ts_exact REAL)"""
        )
        conn.execute("CREATE INDEX idx_gm_user ON group_messages(user_id, group_id)")
        conn.execute(
            """CREATE TABLE bot_messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,
                target_user_id TEXT DEFAULT '', message_id INTEGER,
                text TEXT DEFAULT '', recalled INTEGER DEFAULT 0,
                ts_exact REAL,
                created_at DATETIME DEFAULT (datetime('now','localtime')))"""
        )
        conn.execute(
            "INSERT INTO group_messages (group_id,user_id,user_name,content,message_id)"
            " VALUES ('g1','u1','小明','旧消息',75563830)"
        )
        conn.execute(
            "INSERT INTO bot_messages (group_id,message_id,text,target_user_id)"
            " VALUES ('g1',75563831,'旧机器人消息','2002')"
        )
        conn.commit()
        conn.close()

    def test_both_tables_become_text_and_keep_their_rows(self, tmp_path):
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        self._make_legacy_db(path)
        db = DatabaseManager(path)

        gm_types = {r[1]: (r[2] or "").upper()
                    for r in db.fetch_data("PRAGMA table_info(group_messages)")}
        bot_types = {r[1]: (r[2] or "").upper()
                     for r in db.fetch_data("PRAGMA table_info(bot_messages)")}
        assert gm_types["message_id"] == "TEXT"
        assert bot_types["message_id"] == "TEXT"

        assert db.fetch_data("SELECT COUNT(*) FROM group_messages")[0][0] == 1
        assert db.fetch_data("SELECT COUNT(*) FROM bot_messages")[0][0] == 1
        assert db.fetch_data("SELECT content FROM group_messages")[0][0] == "旧消息"
        assert db.fetch_data("SELECT text FROM bot_messages")[0][0] == "旧机器人消息"

    def test_the_rebuilt_rows_are_still_queryable(self, tmp_path):
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        self._make_legacy_db(path)
        db = DatabaseManager(path)

        assert db.find_quoted("g1", 75563830)["text"] == "旧消息"
        own = db.find_quoted("g1", 75563831)
        assert own["is_own"] is True and own["text"] == "旧机器人消息"
        assert db.fetch_quoted_target("g1", 75563831) == "2002"

    def test_the_group_index_is_recreated(self, tmp_path):
        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        self._make_legacy_db(path)
        db = DatabaseManager(path)
        rows = db.fetch_data(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_gm_user'"
        )
        assert rows, "renaming the table detaches its indexes; it must be recreated"
