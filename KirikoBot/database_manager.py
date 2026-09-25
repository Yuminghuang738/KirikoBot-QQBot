from __future__ import annotations

import difflib
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

VALID_TABLES = {
    "history", "tarot_history", "tarot_content",
    "group_messages", "user_profiles", "tool_usage",
    "reminders", "learning_log", "feature_requests",
    "app_versions", "changelog", "stickers",
    "user_affection", "user_affection_log",
    "feature_settings", "bot_messages", "ai_calls", "group_subscriptions", "profile_history",
    "amp_heads", "app_state", "user_mood",
}


class DatabaseManager:
    def __init__(self, db_file: str = "robot.db") -> None:
        self.db_file = db_file
        self._member_cache: dict[str, list[dict[str, str]]] = {}
        # One connection per thread. Previously every call opened a new
        # sqlite3 connection and — because `with conn:` only commits, it does
        # not close — leaked it until GC. The app runs a 16-thread WSGI pool
        # plus a 12-thread worker pool, so those added up.
        self._local = threading.local()
        self._create_table()

    def get_connect(self) -> sqlite3.Connection:
        """Thread-local connection. Use as a context manager:

            with db.get_connect() as conn:   # commits / rolls back on exit
                ...
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(self.db_file, timeout=15)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._local.conn = conn
            except sqlite3.Error:
                logger.exception("Failed to connect to database %s", self.db_file)
                raise
        return conn

    def close(self) -> None:
        """Close this thread's connection (call on shutdown / in tests)."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    @staticmethod
    def _migrate_user_profiles(connect: sqlite3.Connection) -> None:
        """Rebuild user_profiles with a composite (user_id, group_id) key.

        The original schema was `user_id TEXT NOT NULL UNIQUE` plus a single
        group_id column, so a user active in several groups could only ever
        keep ONE profile — it was overwritten each time they spoke elsewhere,
        and get_group_profiles() silently lost them in the other groups.

        SQLite cannot drop a UNIQUE constraint, so the table is rebuilt. The
        index is dropped explicitly because renaming a table keeps its indexes
        attached to the renamed table, which would make the later
        `CREATE INDEX IF NOT EXISTS` a no-op.
        """
        row = connect.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_profiles'"
        ).fetchone()
        table_sql = (row[0] or "") if row else ""
        if "(user_id, group_id)" in table_sql or "UNIQUE(user_id, group_id)" in table_sql:
            return

        logger.info("Migrating user_profiles to UNIQUE(user_id, group_id)")
        connect.execute("ALTER TABLE user_profiles RENAME TO user_profiles_old")
        connect.execute("DROP INDEX IF EXISTS idx_up_user")
        connect.execute(
            """CREATE TABLE user_profiles(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL,
                group_id     TEXT NOT NULL,
                user_name    TEXT NOT NULL,
                profile_json TEXT NOT NULL DEFAULT '{}',
                message_count INTEGER DEFAULT 0,
                last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                UNIQUE(user_id, group_id)
            )"""
        )
        connect.execute(
            """INSERT OR IGNORE INTO user_profiles
                   (user_id, group_id, user_name, profile_json, message_count, last_updated)
               SELECT user_id, group_id, user_name, profile_json, message_count, last_updated
               FROM user_profiles_old"""
        )
        connect.execute("DROP TABLE user_profiles_old")
        logger.info("user_profiles migration done")

    @staticmethod
    def _column_declared_type(connect: sqlite3.Connection, table: str, column: str) -> str:
        """Declared type of one column, uppercase, or "" when absent/unknown."""
        try:
            rows = connect.execute(f"PRAGMA table_info({table})").fetchall()
        except sqlite3.Error:
            return ""
        for r in rows:
            if r[1] == column:
                return str(r[2] or "").upper()
        return ""

    @classmethod
    def _migrate_message_id_to_text(cls, connect: sqlite3.Connection) -> None:
        """Rebuild tables whose `message_id` is still declared INTEGER.

        QQ 官方平台的消息 id 是形如 ``ROBOT1.0_xxx.yyy!zzz`` 的字符串（含 ``.``
        和 ``!``），不是数字。旧库把 ``message_id`` 声明成 INTEGER；读取路径里
        的 ``int(message_id)`` 会 ValueError，然后被 except 吞成「没找到」——
        症状是引用感知静默失效，而不是报错。

        SQLite 不能修改列类型，而 ``ALTER TABLE ... ADD COLUMN`` 对已存在的列
        是 no-op，所以只能整表重建：按新 schema 建临时表 → 原样搬数据 →
        换名。数据不做任何转换：老库里存的整数 id 搬进 TEXT 列后会按亲和性
        以文本形式存储，查询参数（无论 int 还是 str）也会被亲和性统一成文本
        再比较，所以老数据不会失效。
        """
        cls._rebuild_message_id_table(
            connect,
            table="group_messages",
            create_sql="""CREATE TABLE group_messages(
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id     TEXT NOT NULL,
                user_id      TEXT NOT NULL,
                user_name    TEXT NOT NULL,
                user_role    TEXT DEFAULT '',
                content      TEXT NOT NULL,
                msg_type     TEXT DEFAULT 'text',
                timestamp    DATETIME DEFAULT (datetime('now', 'localtime')),
                message_id   TEXT,
                message_seq  INTEGER,
                reply_to_seq INTEGER,
                ts_exact     REAL
            )""",
            drop_indexes=("idx_gm_user",),
        )
        cls._rebuild_message_id_table(
            connect,
            table="bot_messages",
            create_sql="""CREATE TABLE bot_messages(
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id       TEXT NOT NULL,
                target_user_id TEXT DEFAULT '',
                message_id     TEXT,
                text           TEXT DEFAULT '',
                recalled       INTEGER DEFAULT 0,
                ts_exact       REAL,
                created_at     DATETIME DEFAULT (datetime('now', 'localtime'))
            )""",
            drop_indexes=(),
        )

    @classmethod
    def _rebuild_message_id_table(
        cls, connect: sqlite3.Connection, *, table: str, create_sql: str,
        drop_indexes: tuple[str, ...],
    ) -> None:
        declared = cls._column_declared_type(connect, table, "message_id")
        if not declared or declared == "TEXT":
            return
        logger.info("Migrating %s.message_id from %s to TEXT", table, declared)
        old_cols = [r[1] for r in connect.execute(f"PRAGMA table_info({table})").fetchall()]
        connect.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
        # Renaming keeps the old indexes attached to the renamed table, which
        # would make the later `CREATE INDEX IF NOT EXISTS` a silent no-op.
        for idx in drop_indexes:
            connect.execute(f"DROP INDEX IF EXISTS {idx}")
        connect.execute(create_sql)
        new_cols = [r[1] for r in connect.execute(f"PRAGMA table_info({table})").fetchall()]
        copy_cols = [c for c in new_cols if c in old_cols]
        col_list = ", ".join(copy_cols)
        connect.execute(f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {table}_old")
        connect.execute(f"DROP TABLE {table}_old")
        logger.info("%s.message_id migration done", table)

    def _create_table(self) -> None:
        try:
            with self.get_connect() as connect:
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS history(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        group_id     TEXT,
                        role         TEXT NOT NULL,
                        content      TEXT NOT NULL,
                        tool_calls   TEXT,
                        tool_call_id TEXT,
                        timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: keep the turn's thinking chain so a user can ask
                # "what were you thinking" about the PREVIOUS reply.
                try:
                    connect.execute("ALTER TABLE history ADD COLUMN reasoning TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS tarot_history(
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id   TEXT NOT NULL,
                        card_name TEXT NOT NULL,
                        timestamp DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS tarot_content(
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        card_name TEXT NOT NULL,
                        card_text TEXT NOT NULL,
                        card_path TEXT NOT NULL
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS amp_heads(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        brand      TEXT NOT NULL,
                        model      TEXT NOT NULL,
                        year       TEXT DEFAULT '',
                        origin     TEXT DEFAULT '',
                        kind       TEXT DEFAULT '',
                        power      TEXT DEFAULT '',
                        tubes      TEXT DEFAULT '',
                        tone       TEXT DEFAULT '',
                        price      TEXT DEFAULT '',
                        famous     TEXT DEFAULT '',
                        tip        TEXT DEFAULT '',
                        source     TEXT DEFAULT 'manual',
                        source_url TEXT DEFAULT '',
                        fetched_at TEXT DEFAULT '',
                        UNIQUE(brand, model)
                    )"""
                )
                # Older databases predate the provenance columns.
                existing = {
                    r[1] for r in connect.execute("PRAGMA table_info(amp_heads)")
                }
                for col in ("source", "source_url", "fetched_at"):
                    if col not in existing:
                        default = "'manual'" if col == "source" else "''"
                        connect.execute(
                            f"ALTER TABLE amp_heads ADD COLUMN {col} TEXT DEFAULT {default}"
                        )
                self.seed_amp_heads(connect)
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS app_state(
                        key   TEXT PRIMARY KEY,
                        value TEXT DEFAULT ''
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_mood(
                        user_id    TEXT NOT NULL,
                        group_id   TEXT NOT NULL DEFAULT '',
                        level      INTEGER DEFAULT 0,
                        updated_at DATETIME DEFAULT (datetime('now','localtime')),
                        PRIMARY KEY (user_id, group_id)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS group_messages(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id   TEXT NOT NULL,
                        user_id    TEXT NOT NULL,
                        user_name  TEXT NOT NULL,
                        user_role  TEXT DEFAULT '',
                        content    TEXT NOT NULL,
                        msg_type   TEXT DEFAULT 'text',
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: message id + quote linkage. Enables quote-aware
                # context ("user B is replying to what you said") and recall.
                # message_seq is the QQ seq that a reply segment references;
                # message_id is the platform message id (official platform ids
                # are strings like `ROBOT1.0_xxx.yyy!zzz`, so TEXT — see
                # _migrate_message_id_to_text for old INTEGER columns).
                # ts_exact is a sub-second epoch stamp: `timestamp` only has
                # second resolution, which is too coarse to interleave a bot
                # reply with the member message it answers.
                for col, col_type in [
                    ("message_id", "TEXT"),
                    ("message_seq", "INTEGER"),
                    ("reply_to_seq", "INTEGER"),
                    ("ts_exact", "REAL"),
                ]:
                    try:
                        connect.execute(
                            f"ALTER TABLE group_messages ADD COLUMN {col} {col_type}"
                        )
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS profile_history(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        group_id     TEXT NOT NULL,
                        profile_json TEXT NOT NULL,
                        message_count INTEGER DEFAULT 0,
                        recorded_at  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS group_subscriptions(
                        group_id        TEXT NOT NULL,
                        topic           TEXT NOT NULL,
                        push_time       TEXT DEFAULT '07:00',
                        enabled         INTEGER DEFAULT 1,
                        last_fired_date TEXT DEFAULT '',
                        PRIMARY KEY (group_id, topic)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS ai_calls(
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp         DATETIME DEFAULT (datetime('now', 'localtime')),
                        source            TEXT DEFAULT '',
                        kind              TEXT DEFAULT 'chat',
                        model             TEXT DEFAULT '',
                        group_id          TEXT DEFAULT '',
                        prompt_tokens     INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        reasoning_tokens  INTEGER DEFAULT 0,
                        cache_hit_tokens  INTEGER DEFAULT 0,
                        cache_miss_tokens INTEGER DEFAULT 0,
                        latency_ms        INTEGER DEFAULT 0,
                        success           INTEGER DEFAULT 1,
                        error             TEXT DEFAULT '',
                        utc_hour          INTEGER,
                        utc_weekday       INTEGER
                    )"""
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ai_ts ON ai_calls(timestamp)"
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS bot_messages(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id   TEXT NOT NULL,
                        target_user_id TEXT DEFAULT '',
                        message_id TEXT,
                        text       TEXT DEFAULT '',
                        recalled   INTEGER DEFAULT 0,
                        ts_exact   REAL,
                        created_at DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Older databases predate the addressee column.
                bot_cols = {r[1] for r in connect.execute("PRAGMA table_info(bot_messages)")}
                if "target_user_id" not in bot_cols:
                    connect.execute(
                        "ALTER TABLE bot_messages ADD COLUMN target_user_id TEXT DEFAULT ''"
                    )
                # Older databases declared message_id INTEGER; the official
                # platform uses string ids, so rebuild those tables as TEXT.
                self._migrate_message_id_to_text(connect)
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_profiles(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        group_id     TEXT NOT NULL,
                        user_name    TEXT NOT NULL,
                        profile_json TEXT NOT NULL DEFAULT '{}',
                        message_count INTEGER DEFAULT 0,
                        last_updated DATETIME DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(user_id, group_id)
                    )"""
                )
                self._migrate_user_profiles(connect)
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS reminders(
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id     TEXT NOT NULL,
                        group_id    TEXT,
                        user_name   TEXT NOT NULL,
                        content     TEXT NOT NULL,
                        remind_time TEXT NOT NULL,
                        fired       INTEGER DEFAULT 0,
                        created_at  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: add repeat_daily column for daily recurring reminders
                try:
                    connect.execute(
                        "ALTER TABLE reminders ADD COLUMN repeat_daily INTEGER DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS tool_usage(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        tool_name  TEXT NOT NULL,
                        user_id    TEXT NOT NULL,
                        group_id   TEXT,
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: keep enough of each invocation to replay the chain
                # ("which tool with what args, and what came back"), so the bot
                # can explain what it just did when asked.
                for col, col_type in [
                    ("arguments", "TEXT DEFAULT ''"),
                    ("result", "TEXT DEFAULT ''"),
                    ("reasoning", "TEXT DEFAULT ''"),
                ]:
                    try:
                        connect.execute(f"ALTER TABLE tool_usage ADD COLUMN {col} {col_type}")
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS learning_log(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id    TEXT NOT NULL,
                        note       TEXT NOT NULL,
                        user_msg   TEXT DEFAULT '',
                        ai_text    TEXT DEFAULT '',
                        tool_name  TEXT DEFAULT '',
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: add context columns if they don't exist yet
                for col, col_type in [("user_msg", "TEXT DEFAULT ''"), ("ai_text", "TEXT DEFAULT ''"), ("tool_name", "TEXT DEFAULT ''")]:
                    try:
                        connect.execute(f"ALTER TABLE learning_log ADD COLUMN {col} {col_type}")
                    except sqlite3.OperationalError:
                        pass  # Column already exists
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS feature_requests(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id      TEXT NOT NULL,
                        user_name    TEXT NOT NULL,
                        group_id     TEXT,
                        request_text TEXT NOT NULL,
                        category     TEXT DEFAULT '未分类',
                        priority     TEXT DEFAULT 'normal',
                        status       TEXT DEFAULT 'pending',
                        ai_summary   TEXT DEFAULT '',
                        timestamp    DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS app_versions(
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        version      TEXT NOT NULL UNIQUE,
                        release_date TEXT NOT NULL,
                        description  TEXT DEFAULT '',
                        author       TEXT DEFAULT 'developer',
                        digest_sent  INTEGER DEFAULT 0,
                        created_at   DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Migration: add digest_sent column if not present
                try:
                    connect.execute(
                        "ALTER TABLE app_versions ADD COLUMN digest_sent INTEGER DEFAULT 0"
                    )
                except sqlite3.OperationalError:
                    pass
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS changelog(
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        version_id  INTEGER NOT NULL,
                        entry_type  TEXT NOT NULL DEFAULT 'feature',
                        title       TEXT NOT NULL,
                        description TEXT DEFAULT '',
                        author      TEXT DEFAULT 'developer',
                        created_at  DATETIME DEFAULT (datetime('now', 'localtime')),
                        FOREIGN KEY (version_id) REFERENCES app_versions(id)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS stickers(
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename        TEXT NOT NULL UNIQUE,
                        file_hash       TEXT NOT NULL,
                        category        TEXT DEFAULT '未分类',
                        content_desc    TEXT DEFAULT '',
                        emotion         TEXT DEFAULT '',
                        file_size       INTEGER DEFAULT 0,
                        source_group_id TEXT,
                        source_user_id  TEXT,
                        categorized_at  DATETIME,
                        collected_at    DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_affection(
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id           TEXT NOT NULL,
                        group_id          TEXT NOT NULL,
                        user_name         TEXT NOT NULL,
                        affection_score   REAL DEFAULT 50.0,
                        interaction_count INTEGER DEFAULT 0,
                        positive_count    INTEGER DEFAULT 0,
                        negative_count    INTEGER DEFAULT 0,
                        last_interaction  DATETIME,
                        relationship      TEXT DEFAULT 'neutral',
                        notes             TEXT DEFAULT '',
                        created_at        DATETIME DEFAULT (datetime('now', 'localtime')),
                        updated_at        DATETIME DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(user_id, group_id)
                    )"""
                )
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS user_affection_log(
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id    TEXT NOT NULL,
                        group_id   TEXT NOT NULL,
                        date       TEXT NOT NULL,
                        delta      REAL DEFAULT 0,
                        timestamp  DATETIME DEFAULT (datetime('now', 'localtime'))
                    )"""
                )
                # Per-group / per-user feature toggles. settings_json only stores
                # DISABLED keys; missing key or missing row = enabled.
                connect.execute(
                    """CREATE TABLE IF NOT EXISTS feature_settings(
                        scope_type    TEXT NOT NULL,
                        scope_id      TEXT NOT NULL,
                        settings_json TEXT NOT NULL DEFAULT '{}',
                        updated_at    DATETIME DEFAULT (datetime('now', 'localtime')),
                        PRIMARY KEY (scope_type, scope_id)
                    )"""
                )
                # Index for fast lookups
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gm_user ON group_messages(user_id, group_id)"
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_up_user ON user_profiles(user_id, group_id)"
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ua_user ON user_affection(user_id, group_id)"
                )
                connect.execute(
                    "CREATE INDEX IF NOT EXISTS idx_ual_user ON user_affection_log(user_id, group_id, date)"
                )
        except sqlite3.Error:
            logger.exception("Failed to create database tables")
            raise

    def fetch_data(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        try:
            with self.get_connect() as connect:
                cursor = connect.execute(sql, params)
                return cursor.fetchall()
        except sqlite3.Error:
            logger.exception("Database query failed: %s", sql)
            raise

    def execute_action(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        try:
            with self.get_connect() as connect:
                connect.execute(sql, params)
        except sqlite3.Error:
            logger.exception("Database write failed: %s", sql)
            raise

    def _validate_table(self, table: str) -> None:
        if table not in VALID_TABLES:
            raise ValueError(f"Invalid table name: {table}")

    def takeout(
        self, table: str, column: str,
        table_format: str | None = None, params: tuple[Any, ...] = (),
    ) -> list[tuple[Any, ...]]:
        self._validate_table(table)
        if table_format:
            sql = f"SELECT {column} FROM {table} WHERE {table_format} ORDER BY ID DESC LIMIT 12"
        else:
            sql = f"SELECT {column} FROM {table} ORDER BY ID DESC LIMIT 12"
        return self.fetch_data(sql, params)

    def deposit(
        self, table: str, column: str,
        table_format: str, params: tuple[Any, ...],
    ) -> None:
        self._validate_table(table)
        sql = f"INSERT INTO {table} {column} VALUES {table_format}"
        self.execute_action(sql, params)

    # ── Chat history ───────────────────────────────────

    def deposit_chat_history(
        self, role: str, user_id: str, group_id: str | None,
        content: str, tool_calls: str, tool_call_id: str,
        reasoning: str = "",
    ) -> None:
        self.deposit(
            "history",
            "(role, user_id, group_id, content, tool_calls, tool_call_id, reasoning)",
            "(?, ?, ?, ?, ?, ?, ?)",
            (role, user_id, group_id, content, tool_calls, tool_call_id,
             reasoning[:8000]),
        )

    def get_last_bot_turn(self, user_id: str, group_id: str | None) -> dict[str, Any] | None:
        """The bot's most recent finished reply, with its thinking chain.

        Used by explain_self: when the user asks about "the previous message",
        the current turn has not been saved yet, so the newest assistant row
        *is* the previous reply.
        """
        try:
            rows = self.fetch_data(
                "SELECT content, tool_calls, reasoning, timestamp FROM history "
                "WHERE role = 'assistant' AND user_id = ? AND IFNULL(group_id,'') = ? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, group_id or ""),
            )
        except sqlite3.Error:
            logger.exception("last bot turn query failed")
            return None
        if not rows:
            return None
        return {"content": rows[0][0], "tool_calls": rows[0][1] or "",
                "reasoning": rows[0][2] or "", "timestamp": rows[0][3]}

    def deposit_tarot_history(self, user_id: str, card_name: str) -> None:
        self.deposit("tarot_history", "(user_id, card_name)", "(?, ?)", (user_id, card_name))

    def takeout_chat_history(self, user_id: str, group_id: str | None) -> list[tuple[Any, ...]]:
        if group_id:
            rows = self.takeout(
                "history", "role, content, tool_calls, tool_call_id",
                "user_id = ? AND group_id = ?", (user_id, group_id),
            )
        else:
            # 私聊作用域。**必须同时容忍 NULL 和空串**：官方平台的 C2C 事件里
            # `group_openid` 缺失，`IncomingMessage.group_id` 是 `""`，全项目
            # 其他地方（user_mood / user_affection / ai_metrics…）也都用
            # `group_id or ""` 归一化，于是写进去的是 `""`。而这里以前只查
            # `IS NULL`（OneBot 时代私聊的 group_id 确实是 NULL），两边对不上，
            # 结果是**私聊记忆完全读不出来** —— 每句话都被当成全新对话，
            # 而且不报错、不日志，只有真去翻库才会发现。
            rows = self.takeout(
                "history", "role, content, tool_calls, tool_call_id",
                "user_id = ? AND (group_id IS NULL OR group_id = '')", (user_id,),
            )
        rows.reverse()
        return rows

    def takeout_tarot_history(self, user_id: str) -> list[tuple[Any, ...]]:
        return self.takeout("tarot_history", "card_name, timestamp", "user_id = ?", (user_id,))

    # ── Group message recording ────────────────────────

    def record_group_message(
        self, group_id: str, user_id: str, user_name: str,
        content: str, user_role: str = "", msg_type: str = "text",
        message_id: str | None = None, message_seq: int | None = None,
        reply_to_seq: int | None = None,
    ) -> None:
        self.deposit(
            "group_messages",
            "(group_id, user_id, user_name, user_role, content, msg_type, "
            "message_id, message_seq, reply_to_seq, ts_exact)",
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (group_id, user_id, user_name, user_role, content, msg_type,
             message_id, message_seq, reply_to_seq, time.time()),
        )

    def get_user_messages(
        self, user_id: str, group_id: str, limit: int = 50,
    ) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT content, timestamp FROM group_messages "
            "WHERE user_id = ? AND group_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, group_id, limit),
        )

    def get_recent_group_messages(
        self, group_id: str, limit: int = 100,
    ) -> list[tuple[Any, ...]]:
        if group_id:
            return self.fetch_data(
                "SELECT user_id, user_name, content, timestamp FROM group_messages "
                "WHERE group_id = ? ORDER BY id DESC LIMIT ?",
                (group_id, limit),
            )
        return self.fetch_data(
            "SELECT user_id, user_name, content, timestamp FROM group_messages "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    # ── Bot's own messages (recall + "is this mine?") ────

    def record_bot_message(self, group_id: str, message_id: str | None,
                           text: str = "", target_user_id: str = "") -> None:
        """Record one of our own messages, and who it was addressed to.

        `target_user_id` is the person being replied to (from the outgoing
        `at` segment). Without it there is no way to tell "B quoting what I
        said to A" from "A quoting what I said to A", and the bot treats the
        new speaker as the old one.
        """
        if message_id is None:
            return
        self.execute_action(
            "INSERT INTO bot_messages (group_id, message_id, text, ts_exact, target_user_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (group_id, message_id, text[:500], time.time(), str(target_user_id or "")),
        )

    def get_last_bot_message(self, group_id: str, max_age_seconds: int = 110) -> dict[str, Any] | None:
        """Most recent message the bot sent to this group, within the recall window.

        QQ lets a normal member recall their own message for roughly two
        minutes; older ids are useless, so they are filtered out here rather
        than failing at the API.
        """
        try:
            rows = self.fetch_data(
                "SELECT message_id, text, created_at FROM bot_messages "
                "WHERE group_id = ? AND recalled = 0 AND message_id IS NOT NULL "
                "AND created_at >= datetime('now', 'localtime', ?) "
                "ORDER BY id DESC LIMIT 1",
                (group_id, f"-{int(max_age_seconds)} seconds"),
            )
        except sqlite3.Error:
            logger.exception("get_last_bot_message failed")
            return None
        if not rows:
            return None
        return {"message_id": rows[0][0], "text": rows[0][1], "created_at": rows[0][2]}

    def mark_bot_message_recalled(self, message_id: str) -> None:
        self.execute_action(
            "UPDATE bot_messages SET recalled = 1 WHERE message_id = ?", (message_id,)
        )

    @staticmethod
    def _clamp_int(value: Any, default: int, lo: int, hi: int) -> int:
        """Coerce a caller-supplied number into range.

        `value or default` would be wrong here: 0 is a legitimate (if useless)
        input, and the model may also hand us a string.
        """
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = default
        return max(lo, min(n, hi))

    # Patience ladder. Counts come from `history`, not `group_messages`:
    # history only holds messages the bot actually processed (i.e. addressed to
    # it), so a user chatting with other people does not count as pestering it.
    # The escalation is tsundere, not anger: the bot gets more sulky and
    # eventually slacks off, but never turns genuinely mean.
    PESTER_LEVELS = (
        (0, "正常"),
        (2, "有点小情绪"),
        (4, "开始傲娇"),
        (6, "傲娇加倍"),
        (9, "摆烂不干了"),
    )
    _REPEAT_RATIO = 0.8   # how similar a message must be to count as "the same"

    def get_recent_pestering(self, user_id: str, group_id: str | None,
                             minutes: int = 10, text: str = "") -> dict[str, Any]:
        """How hard this user has been leaning on the bot lately.

        Progressive emotion needs a *memory of how many times* it has been
        asked, and each request is otherwise independent — the model cannot
        count what it cannot see. So the count and the repeat count are
        computed here and handed to the model as a fact.

        Returns {"count", "repeats", "level", "label"}. `count` excludes the
        message being answered (it is not saved yet).
        """
        minutes = self._clamp_int(minutes, 10, 1, 24 * 60)
        try:
            rows = self.fetch_data(
                "SELECT content FROM history "
                "WHERE role = 'user' AND user_id = ? AND IFNULL(group_id,'') = ? "
                "AND timestamp >= datetime('now','localtime',?) "
                "ORDER BY id DESC LIMIT 20",
                (user_id, group_id or "", f"-{minutes} minutes"),
            )
        except Exception:
            # Best-effort: a mood signal that fails must not make the bot angry,
            # nor break the reply it was only decorating.
            logger.debug("pestering query failed", exc_info=True)
            return {"count": 0, "repeats": 0, "level": 0, "label": "正常"}

        past = [str(r[0] or "") for r in rows]
        count = len(past)

        needle = self._squeeze(text)
        repeats = 0
        if needle:
            for old in past:
                old_n = self._squeeze(old)
                if old_n and difflib.SequenceMatcher(None, needle, old_n).ratio() >= self._REPEAT_RATIO:
                    repeats += 1

        level = 0
        for idx, (threshold, _label) in enumerate(self.PESTER_LEVELS):
            if count >= threshold:
                level = idx
        # Repeats are the stronger signal: asking the same thing again is more
        # annoying than merely talking a lot.
        level = max(level, min(repeats, len(self.PESTER_LEVELS) - 1))
        return {"count": count, "repeats": repeats, "level": level,
                "label": self.PESTER_LEVELS[level][1]}

    @staticmethod
    def _squeeze(text: str) -> str:
        """Lowercase, punctuation-free — so "在吗？" and "在吗" are the same."""
        return re.sub(r"[\s\W_]+", "", (text or "").lower())

    # ── Mood with a cooldown ──────────────────────────────
    MAX_MOOD_LEVEL = 4

    def _read_mood(self, user_id: str, group_id: str | None) -> tuple[int, str]:
        try:
            rows = self.fetch_data(
                "SELECT level, updated_at FROM user_mood "
                "WHERE user_id = ? AND group_id = ?", (user_id, group_id or ""))
        except Exception:
            return 0, ""
        if not rows:
            return 0, ""
        return int(rows[0][0] or 0), str(rows[0][1] or "")

    def _write_mood(self, user_id: str, group_id: str | None, level: int) -> None:
        try:
            if level <= 0:
                self.execute_action(
                    "DELETE FROM user_mood WHERE user_id = ? AND group_id = ?",
                    (user_id, group_id or ""))
                return
            self.execute_action(
                "INSERT INTO user_mood (user_id, group_id, level, updated_at) "
                "VALUES (?, ?, ?, datetime('now','localtime')) "
                "ON CONFLICT(user_id, group_id) DO UPDATE SET "
                "level = excluded.level, updated_at = excluded.updated_at",
                (user_id, group_id or "", level))
        except Exception:
            logger.debug("mood write failed", exc_info=True)

    def get_today_tarot(self, user_id: str) -> dict[str, Any] | None:
        """The card this user already drew today, with its text and image.

        One card per person per day: drawing again would make the reading
        meaningless ("the cards said X, now they say Y"), so a repeat shows
        the *original* card rather than a fresh one — good or bad, it stands.
        """
        try:
            rows = self.fetch_data(
                "SELECT card_name, timestamp FROM tarot_history "
                "WHERE user_id = ? AND date(timestamp) = date('now','localtime') "
                "ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
        except Exception:
            # Best-effort by design: if this fails we fall through to drawing a
            # card. That is the wrong side of the daily limit to err on, but
            # refusing every reading because of a transient DB error is worse.
            logger.exception("today's tarot query failed")
            return None
        if not rows:
            return None

        name = str(rows[0][0] or "").strip()
        card = {"card_name": name, "timestamp": rows[0][1],
                "card_text": "", "card_path": ""}
        try:
            detail = self.fetch_data(
                "SELECT card_text, card_path FROM tarot_content "
                "WHERE TRIM(card_name) = ? LIMIT 1", (name,))
        except sqlite3.Error:
            logger.debug("tarot card lookup failed", exc_info=True)
            detail = []
        if detail:
            card["card_text"] = detail[0][0] or ""
            card["card_path"] = detail[0][1] or ""
        return card

    def get_mood(self, user_id: str, group_id: str | None, text: str = "",
                 pressure_minutes: int = 10, cooldown_minutes: int = 30) -> dict[str, Any]:
        """The bot's current temper toward this user, and how it is cooling.

        Mood has to be *state*, not just a property of the last few messages.
        Otherwise someone who has just been driven up the wall is instantly
        pleasant again the moment the counting window rolls over, which is not
        how a person works — and a temper that never subsides is worse.

        So the stored level decays linearly to zero over `cooldown_minutes`,
        and the current message's pressure sets a floor. Both directions are
        modelled: quick to rise, slow-ish to forgive.
        """
        pressure = self.get_recent_pestering(
            user_id, group_id, minutes=pressure_minutes, text=text)

        stored, updated = self._read_mood(user_id, group_id)
        cooldown = max(1, int(cooldown_minutes or 30))
        decayed = 0
        cooling = False
        if stored > 0 and updated:
            try:
                elapsed = (datetime.now() - datetime.strptime(
                    updated, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
            except ValueError:
                elapsed = 0.0
            # Decay in whole steps. Truncating the remaining level instead
            # would shave a level off the moment a second had passed, so a
            # freshly-earned 4 read back as a 3.
            step = cooldown / float(self.MAX_MOOD_LEVEL)
            steps = int(elapsed // step)
            decayed = max(0, min(stored, stored - steps))
            cooling = decayed < stored

        level = max(decayed, int(pressure["level"]))
        level = max(0, min(level, self.MAX_MOOD_LEVEL))
        self._write_mood(user_id, group_id, level)

        return {
            "level": level,
            "label": self.PESTER_LEVELS[level][1],
            "cooling": cooling and level > 0,
            "count": pressure["count"],
            "repeats": pressure["repeats"],
        }

    def find_quoted(self, group_id: str | None, message_id: Any,
                    quoted_text: str = "") -> dict[str, Any] | None:
        """Resolve a quoted message to its text, author, and addressee.

        Why this exists: on OneBot the `reply` segment carried **only the id**
        (no text, no sender), so the quoted content had to be reconstructed
        from our own tables.

        官方平台不一样：事件里**直接带**被引正文和作者昵称（见
        `qq_official._extract_quote`），但它带不出最要紧的那一条 ——
        「那句话当初是说给谁的」。所以要拿 id 回来查 `bot_messages`。

        `message_id` is used verbatim: the official platform's ids are strings,
        so casting with `int()` would raise and silently degrade every lookup
        into a miss. None / "" still short-circuits, so the value can never
        turn into a SQL `= NULL` (which matches nothing, or worse, everything).

        `quoted_text` 是**兜底**，而且是常用路径：官方事件里引用带的索引是
        `ref_msg_idx=TMP_...` 这种形式，和我们存的 `ROBOT1.0_...` 消息 id
        **不是同一套编号**（见官方文档 group_at_message_create 的示例三），
        所以按 id 查多半查不到。但事件给了被引正文，而机器人自己的发言正文
        我们是有记录的 —— 用正文精确匹配就能既认出「这是我说的」，又拿到
        收件人。阈值沿用 `QQOfficialClient.is_own_message` 的 6 字符护栏，
        避免一句话短到到处都能撞上。
        """
        if message_id is not None and message_id != "":
            found = self._find_quoted_by_id(group_id, message_id)
            if found is not None:
                return found

        needle = " ".join((quoted_text or "").split())
        if len(needle) >= 6:
            return self._find_quoted_by_text(group_id, needle)
        return None

    def _find_quoted_by_id(self, group_id: str | None, mid: Any) -> dict[str, Any] | None:
        # The bot's lines first: telling "they are quoting ME" apart from
        # "they are quoting someone else" is the whole point of the feature.
        #
        # Group-scoped first, then a global fallback. Scoping alone fails
        # *silently* whenever the group id does not match byte-for-byte, and a
        # dropped quote note is invisible — the bot just answers as if nothing
        # had been quoted. QQ message ids are unique account-wide, so the
        # fallback is safe and turns a silent miss into a hit.
        lookups = (
            ("SELECT text, target_user_id FROM bot_messages WHERE message_id = ?"
             " AND group_id = ? ORDER BY id DESC LIMIT 1", True, True),
            ("SELECT text, target_user_id FROM bot_messages WHERE message_id = ?"
             " ORDER BY id DESC LIMIT 1", True, False),
            ("SELECT content, user_name FROM group_messages WHERE message_id = ?"
             " AND group_id = ? ORDER BY id DESC LIMIT 1", False, True),
            ("SELECT content, user_name FROM group_messages WHERE message_id = ?"
             " ORDER BY id DESC LIMIT 1", False, False),
        )
        for sql, own, scoped in lookups:
            if scoped and group_id is None:
                continue
            params = (mid, group_id) if scoped else (mid,)
            try:
                rows = self.fetch_data(sql, params)
            except sqlite3.Error:
                logger.debug("find_quoted lookup failed", exc_info=True)
                continue
            if not rows:
                continue
            if own:
                target_id = str(rows[0][1] or "") if len(rows[0]) > 1 else ""
                return {"text": rows[0][0] or "", "user_name": "", "is_own": True,
                        "target_name": self._resolve_user_name(group_id, target_id)}
            return {"text": rows[0][0] or "", "user_name": rows[0][1] or "",
                    "is_own": False, "target_name": ""}
        return None

    # 正文兜底匹配时最多回看多少条机器人发言。需要扫描而不是直接 SQL 相等，
    # 是因为要按归一化后的空白比较（见 _scan_recent_bot_texts）；取这个数量
    # 是为了给扫描封顶，正常使用远达不到。
    QUOTED_TEXT_SCAN = 200

    def _find_quoted_by_text(self, group_id: str | None,
                             needle: str) -> dict[str, Any] | None:
        """按正文反查**机器人自己**说过的话（拿回收件人）。

        只在 id 查不到时走这里。不查 `group_messages`：那一边的正文是群友发的，
        而这里要回答的问题是「被引的是不是我自己说的、说给谁的」。正文已经由
        事件给出了，不需要再还原一遍。

        比较分两步，因为两边的空白**不一定一样**：`needle` 是归一化过的
        （`" ".join(text.split())`，把换行和连续空格压成单个空格），而库里存的是
        发送时的原文。所以先按原文做一次精确匹配（绝大多数消息是单行、没有多余
        空格，这一步就命中），没命中再取最近的一批在 Python 侧归一化后比较 ——
        不这么做的话，一条带换行的发言永远匹配不上。
        """
        base = ("SELECT text, target_user_id FROM bot_messages "
                "WHERE recalled = 0 AND text = ?")
        params: list[Any] = [needle]
        if group_id is not None:
            base += " AND group_id = ?"
            params.append(group_id)
        try:
            rows = self.fetch_data(base + " ORDER BY id DESC LIMIT 1", tuple(params))
            if not rows:
                rows = self._scan_recent_bot_texts(group_id, needle)
        except sqlite3.Error:
            logger.debug("find_quoted text lookup failed", exc_info=True)
            return None
        if not rows:
            return None
        target_id = str(rows[0][1] or "")
        return {"text": rows[0][0] or needle, "user_name": "", "is_own": True,
                "target_name": self._resolve_user_name(group_id, target_id)}

    def _scan_recent_bot_texts(self, group_id: str | None,
                               needle: str) -> list[tuple[Any, ...]]:
        """Recent bot lines, compared with whitespace normalized on our side."""
        sql = "SELECT text, target_user_id FROM bot_messages WHERE recalled = 0"
        params: list[Any] = []
        if group_id is not None:
            sql += " AND group_id = ?"
            params.append(group_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(self.QUOTED_TEXT_SCAN)
        rows = self.fetch_data(sql, tuple(params))
        for row in rows:
            if " ".join(str(row[0] or "").split()) == needle:
                return [row]
        return []

    def fetch_quoted_target(self, group_id: str | None, message_id: Any) -> str:
        """Raw addressee id recorded for one of our own messages (debug/tests)."""
        if message_id is None or message_id == "":
            return ""
        try:
            rows = self.fetch_data(
                "SELECT target_user_id FROM bot_messages WHERE message_id = ? "
                "AND (? IS NULL OR group_id = ?) ORDER BY id DESC LIMIT 1",
                (message_id, group_id, group_id))
        except Exception:
            return ""
        return str(rows[0][0] or "") if rows else ""

    def _resolve_user_name(self, group_id: str | None, user_id: str) -> str:
        """Best-effort display name for a QQ number, from what we have seen."""
        if not user_id:
            return ""
        try:
            rows = self.fetch_data(
                "SELECT user_name FROM group_messages WHERE user_id = ? "
                "AND (? IS NULL OR group_id = ?) ORDER BY id DESC LIMIT 1",
                (user_id, group_id, group_id))
        except Exception:
            logger.debug("user name lookup failed", exc_info=True)
            return ""
        return str(rows[0][0] or "") if rows else ""

    # 群推送订阅的读写方法（get_subscriptions / set_subscription /
    # delete_subscription / due_subscriptions / mark_subscription_fired）已随
    # 主动推送一起删除：订阅靠「到点主动往群里发消息」，官方平台没有这个能力。
    # 表 `group_subscriptions` 本身保留（不 DROP）——purge_group 的表清单里还列着它，
    # 旧库也有这张表，删表会让旧库的整群清除报错。

    def get_profile_history(self, user_id: str, group_id: str,
                            limit: int = 3) -> list[dict[str, Any]]:
        """Earlier profile snapshots, newest first."""
        limit = self._clamp_int(limit, 3, 1, 10)
        try:
            rows = self.fetch_data(
                "SELECT profile_json, message_count, recorded_at FROM profile_history "
                "WHERE user_id = ? AND group_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, group_id, limit),
            )
        except sqlite3.Error:
            return []
        import json as _json
        out = []
        for pj, cnt, ts in rows:
            try:
                parsed = _json.loads(pj)
            except (ValueError, TypeError):
                continue
            out.append({"profile": parsed, "message_count": cnt, "recorded_at": ts})
        return out

    # ── AI usage metrics ─────────────────────────────────

    # Peak hours are 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri; off-peak is half.
    _PEAK_HOURS = (1, 2, 3, 6, 7, 8, 9)

    @classmethod
    def _is_peak(cls, utc_hour: Any, utc_weekday: Any) -> bool:
        try:
            hour, weekday = int(utc_hour), int(utc_weekday)
        except (TypeError, ValueError):
            return False
        return weekday < 5 and hour in cls._PEAK_HOURS

    @classmethod
    def estimate_cost_usd(cls, *, cache_hit: int, cache_miss: int, output: int,
                          utc_hour: Any = None, utc_weekday: Any = None) -> float:
        """Cost estimate in USD from Config's per-1M rates (off-peak halved)."""
        from config import Config

        factor = 1.0 if cls._is_peak(utc_hour, utc_weekday) else 0.5
        return (
            cache_hit / 1_000_000 * Config.AI_PRICE_CACHE_HIT
            + cache_miss / 1_000_000 * Config.AI_PRICE_CACHE_MISS
            + output / 1_000_000 * Config.AI_PRICE_OUTPUT
        ) * factor

    def record_ai_call(self, entry: dict[str, Any]) -> None:
        """Append one instrumented API call (see ai_metrics.record)."""
        cols = ("source", "kind", "model", "group_id", "prompt_tokens",
                "completion_tokens", "reasoning_tokens", "cache_hit_tokens",
                "cache_miss_tokens", "latency_ms", "success", "error",
                "utc_hour", "utc_weekday")
        self.deposit(
            "ai_calls",
            "(" + ", ".join(cols) + ")",
            "(" + ", ".join("?" * len(cols)) + ")",
            tuple(entry.get(c) for c in cols),
        )

    def get_ai_metrics(self, hours: int = 24) -> dict[str, Any]:
        """Aggregated AI usage for the dashboard: volume, latency, cost, errors."""
        hours = self._clamp_int(hours, 24, 1, 24 * 30)
        try:
            rows = self.fetch_data(
                "SELECT timestamp, source, kind, model, prompt_tokens, "
                "       completion_tokens, reasoning_tokens, cache_hit_tokens, "
                "       cache_miss_tokens, latency_ms, success, error, "
                "       utc_hour, utc_weekday "
                "FROM ai_calls WHERE timestamp >= datetime('now','localtime',?) "
                "ORDER BY id DESC LIMIT 20000",
                (f"-{hours} hours",),
            )
        except sqlite3.Error:
            logger.exception("ai metrics query failed")
            rows = []

        empty = {
            "hours": hours,
            "totals": {"calls": 0, "failed": 0, "success_rate": 100.0,
                       "prompt_tokens": 0, "completion_tokens": 0,
                       "reasoning_tokens": 0, "cache_hit_tokens": 0,
                       "cache_miss_tokens": 0, "tokens": 0, "cost_usd": 0.0},
            "latency": {"avg_ms": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0},
            "by_source": [],
            # Always 24 buckets so consumers never special-case the empty shape.
            "hourly": [{"hour": h, "calls": 0, "tokens": 0} for h in range(24)],
            "recent_errors": [],
        }
        if not rows:
            return empty

        totals = {"calls": len(rows), "failed": 0, "prompt_tokens": 0,
                  "completion_tokens": 0, "reasoning_tokens": 0,
                  "cache_hit_tokens": 0, "cache_miss_tokens": 0, "cost_usd": 0.0}
        latencies: list[int] = []
        by_source: dict[str, dict[str, Any]] = {}
        hourly = {h: {"hour": h, "calls": 0, "tokens": 0} for h in range(24)}
        errors: list[dict[str, Any]] = []

        for (ts, source, kind, model, pt, ct, rt, hit, miss, latency,
             success, error, uhour, uwday) in rows:
            pt, ct, rt = int(pt or 0), int(ct or 0), int(rt or 0)
            hit, miss = int(hit or 0), int(miss or 0)
            latency = int(latency or 0)
            tokens = pt + ct
            cost = self.estimate_cost_usd(cache_hit=hit, cache_miss=miss,
                                          output=ct, utc_hour=uhour, utc_weekday=uwday)

            totals["prompt_tokens"] += pt
            totals["completion_tokens"] += ct
            totals["reasoning_tokens"] += rt
            totals["cache_hit_tokens"] += hit
            totals["cache_miss_tokens"] += miss
            totals["cost_usd"] += cost
            if not success:
                totals["failed"] += 1
                if len(errors) < 10:
                    errors.append({"timestamp": ts, "source": source or "?",
                                   "error": (error or "")[:160]})
            if success:
                latencies.append(latency)

            bucket = by_source.setdefault(source or "unknown", {
                "source": source or "unknown", "calls": 0, "failed": 0,
                "tokens": 0, "cost_usd": 0.0, "latency_ms": 0,
            })
            bucket["calls"] += 1
            bucket["failed"] += 0 if success else 1
            bucket["tokens"] += tokens
            bucket["cost_usd"] += cost
            bucket["latency_ms"] += latency

            hour = int(str(ts)[11:13]) if ts else 0
            if 0 <= hour < 24:
                hourly[hour]["calls"] += 1
                hourly[hour]["tokens"] += tokens

        latencies.sort()
        def pct(p: float) -> int:
            if not latencies:
                return 0
            idx = min(len(latencies) - 1, int(round((len(latencies) - 1) * p)))
            return latencies[idx]

        for bucket in by_source.values():
            bucket["avg_ms"] = round(bucket["latency_ms"] / bucket["calls"]) if bucket["calls"] else 0
            bucket["cost_usd"] = round(bucket["cost_usd"], 4)
            bucket.pop("latency_ms", None)

        totals["tokens"] = totals["prompt_tokens"] + totals["completion_tokens"]
        totals["cost_usd"] = round(totals["cost_usd"], 4)
        totals["success_rate"] = round(
            (totals["calls"] - totals["failed"]) / totals["calls"] * 100, 1
        ) if totals["calls"] else 100.0

        return {
            "hours": hours,
            "totals": totals,
            "latency": {
                "avg_ms": round(sum(latencies) / len(latencies)) if latencies else 0,
                "p50_ms": pct(0.50),
                "p95_ms": pct(0.95),
                "max_ms": latencies[-1] if latencies else 0,
            },
            "by_source": sorted(by_source.values(), key=lambda b: b["calls"], reverse=True),
            "hourly": [hourly[h] for h in range(24)],
            "recent_errors": errors,
        }

    def clean_orphaned_history(self, user_id: str, group_id: str | None) -> int:
        """Remove assistant messages with tool_calls but no follow-up tool response.
        Returns number of rows deleted."""
        try:
            # Find assistant rows that have tool_calls but no matching tool row after them
            if group_id:
                rows = self.fetch_data(
                    "SELECT id, tool_calls FROM history WHERE user_id=? AND group_id=? ORDER BY id",
                    (user_id, group_id),
                )
            else:
                rows = self.fetch_data(
                    "SELECT id, tool_calls FROM history WHERE user_id=? AND group_id IS NULL ORDER BY id",
                    (user_id,),
                )
            deleted = 0
            for i, (row_id, tc) in enumerate(rows):
                if tc and i + 1 < len(rows):
                    next_row = rows[i + 1]
                    # Next row should be a tool response; if tool_calls is empty, it's orphaned
                    # We can't easily check the next row's role here, so just flag rows
                    pass
                # Simple approach: delete tool rows with empty tool_call_id
                if not tc:  # no tool_calls, skip
                    continue
            return deleted
        except Exception:
            logger.exception("clean_orphaned_history failed")
            return 0

    def validate_and_clean_history(self, user_id: str, group_id: str | None) -> None:
        """Remove history entries that would cause DeepSeek 400 errors."""
        try:
            if group_id:
                rows = self.fetch_data(
                    "SELECT id, role, tool_calls, tool_call_id FROM history "
                    "WHERE user_id=? AND group_id=? ORDER BY id",
                    (user_id, group_id),
                )
            else:
                rows = self.fetch_data(
                    "SELECT id, role, tool_calls, tool_call_id FROM history "
                    "WHERE user_id=? AND group_id IS NULL ORDER BY id",
                    (user_id,),
                )
            ids_to_delete: list[int] = []

            for i, row in enumerate(rows):
                row_id, role, tc, tci = row[0], row[1], row[2], row[3]
                if role == "assistant" and tc:
                    nxt = rows[i + 1] if i + 1 < len(rows) else None
                    # nxt = (id, role, tool_calls, tool_call_id), index 1=role, 3=tci
                    if not nxt or nxt[1] != "tool" or not nxt[3]:
                        ids_to_delete.append(row_id)
                elif role == "tool":
                    if not tci:
                        ids_to_delete.append(row_id)
                    elif i == 0:
                        ids_to_delete.append(row_id)
                    else:
                        prev = rows[i - 1]
                        if prev[1] != "assistant" or not prev[2]:
                            ids_to_delete.append(row_id)

            for rid in ids_to_delete:
                self.execute_action("DELETE FROM history WHERE id=?", (rid,))
            if ids_to_delete:
                logger.warning(
                    "Cleaned %d orphaned history rows for user %s", len(ids_to_delete), user_id,
                )
        except Exception:
            logger.exception("validate_and_clean_history failed")

    # ── Group member cache ─────────────────────────────

    def seed_group_members(
        self, group_id: str, members: list[dict[str, Any]],
    ) -> None:
        """Cache a group member list.

        以前这些数据来自 OneBot 的成员列表接口。官方平台**没有成员列表接口**
        （`QQOfficialClient.get_group_member_list` 恒返回空），所以这个缓存现在
        基本是空的，`find_member_by_name` 只能靠已经记录下来的消息里出现过的昵称
        来认人。留着这个接口是为了不改调用方签名。
        """
        self._member_cache[group_id] = [
            {
                "user_id": str(m.get("user_id", "")),
                "user_name": str(m.get("nickname", "") or m.get("card", "") or ""),
                "role": str(m.get("role", "")),
            }
            for m in members
            if m.get("user_id")
        ]
        logger.info("Cached %d members for group %s", len(self._member_cache[group_id]), group_id)

    def find_member_by_name(
        self, group_id: str, name: str,
    ) -> tuple[str | None, str | None]:
        """Search cached members + DB for a name. Returns (qq, display_name)."""
        # Try DB first (real messages with accurate names)
        result = self.find_user_by_name(group_id, name)
        if result:
            # Get QQ number from DB
            rows = self.fetch_data(
                "SELECT user_id FROM group_messages WHERE group_id=? AND user_name=? ORDER BY id DESC LIMIT 1",
                (group_id, result),
            )
            if rows:
                return (rows[0][0], result)

        # Try cached member list
        cached = self._member_cache.get(group_id, [])
        for m in cached:
            if name in m["user_name"] or m["user_name"] == name:
                return (m["user_id"], m["user_name"])

        return (None, None)

    def find_member_by_role(
        self, group_id: str, role: str,
    ) -> tuple[str | None, str | None]:
        """Search cached members + DB for a role. Returns (qq, display_name)."""
        # Try DB first
        result = self.find_user_by_role(group_id, role)
        if result:
            rows = self.fetch_data(
                "SELECT user_id FROM group_messages WHERE group_id=? AND user_name=? ORDER BY id DESC LIMIT 1",
                (group_id, result),
            )
            if rows:
                return (rows[0][0], result)

        # Try cached member list
        cached = self._member_cache.get(group_id, [])
        for m in cached:
            if m["role"] == role:
                return (m["user_id"], m["user_name"])
        # Owner fallback for admin role
        if role == "admin":
            for m in cached:
                if m["role"] == "owner":
                    return (m["user_id"], m["user_name"])

        return (None, None)

    def find_user_by_role(
        self, group_id: str, role: str,
    ) -> str | None:
        """Find a user in group by role (owner/admin)."""
        try:
            rows = self.fetch_data(
                "SELECT DISTINCT user_name FROM group_messages "
                "WHERE group_id=? AND user_role=? ORDER BY id DESC LIMIT 1",
                (group_id, role),
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def find_user_by_name(
        self, group_id: str, name: str,
    ) -> str | None:
        """Fuzzy find a user name in group messages."""
        try:
            rows = self.fetch_data(
                "SELECT user_name, COUNT(*) as cnt FROM group_messages "
                "WHERE group_id=? AND (user_name LIKE ? OR user_name = ?) "
                "GROUP BY user_name ORDER BY cnt DESC LIMIT 3",
                (group_id, f"%{name}%", name),
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def get_active_users(
        self, group_id: str, min_messages: int = 10,
    ) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT user_id, user_name, COUNT(*) as cnt FROM group_messages "
            "WHERE group_id = ? GROUP BY user_id HAVING cnt >= ? ORDER BY cnt DESC",
            (group_id, min_messages),
        )

    def get_latest_user_name(self, user_id: str, group_id: str | None = None) -> str | None:
        """Return the most recent user_name for a user_id from group_messages."""
        if group_id:
            rows = self.fetch_data(
                "SELECT user_name FROM group_messages WHERE user_id=? AND group_id=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id, group_id),
            )
        else:
            rows = self.fetch_data(
                "SELECT user_name FROM group_messages WHERE user_id=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id,),
            )
        return rows[0][0] if rows else None

    # ── User profiles ──────────────────────────────────

    def save_user_profile(
        self, user_id: str, group_id: str, user_name: str,
        profile_json: str, message_count: int,
    ) -> None:
        # Use the latest user_name from group_messages if available
        latest = self.get_latest_user_name(user_id, group_id)
        effective_name = latest or user_name
        # Long-term memory: keep what we believed before, so the bot can say
        # "you mentioned X before" instead of only knowing the latest snapshot.
        try:
            previous = self.fetch_data(
                "SELECT profile_json FROM user_profiles WHERE user_id = ? AND group_id = ?",
                (user_id, group_id),
            )
            if previous and previous[0][0] and previous[0][0] != profile_json:
                self.execute_action(
                    "INSERT INTO profile_history (user_id, group_id, profile_json, message_count) "
                    "VALUES (?, ?, ?, ?)",
                    (user_id, group_id, previous[0][0], message_count),
                )
        except sqlite3.Error:
            logger.debug("profile history snapshot failed", exc_info=True)

        self.execute_action(
            "INSERT INTO user_profiles (user_id, group_id, user_name, profile_json, message_count, last_updated) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime')) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET "
            "user_name=excluded.user_name, profile_json=excluded.profile_json, "
            "message_count=excluded.message_count, last_updated=datetime('now', 'localtime')",
            (user_id, group_id, effective_name, profile_json, message_count),
        )

    def get_user_profile(self, user_id: str, group_id: str | None = None) -> dict[str, Any] | None:
        """Profile for a user — scoped to a group when one is given.

        Profiles are per (user, group); omitting group_id falls back to the
        most recently updated one for backwards compatibility.
        """
        cols = "SELECT profile_json, user_name, message_count, last_updated, group_id FROM user_profiles WHERE user_id = ?"
        if group_id:
            rows = self.fetch_data(cols + " AND group_id = ?", (user_id, group_id))
        else:
            rows = self.fetch_data(cols + " ORDER BY last_updated DESC LIMIT 1", (user_id,))
        if not rows:
            return None
        import json
        try:
            profile = json.loads(rows[0][0])
        except (json.JSONDecodeError, TypeError):
            profile = {}
        # Resolve current user name from group_messages (handles nick changes)
        latest_name = self.get_latest_user_name(user_id, rows[0][4])
        return {
            "profile": profile,
            "user_name": latest_name or rows[0][1],
            "message_count": rows[0][2],
            "last_updated": rows[0][3],
        }

    # ── Tool usage tracking ────────────────────────────

    def record_tool_usage(self, tool_name: str, user_id: str, group_id: str | None,
                          arguments: str = "", result: str = "",
                          reasoning: str = "") -> None:
        self.deposit(
            "tool_usage",
            "(tool_name, user_id, group_id, arguments, result, reasoning)",
            "(?, ?, ?, ?, ?, ?)",
            (tool_name, user_id, group_id, arguments[:500], result[:500],
             reasoning[:2000]),
        )

    def get_recent_tool_chain(self, group_id: str | None, user_id: str,
                              limit: int = 5) -> list[dict[str, Any]]:
        """The most recent tool invocations for a user, oldest-first.

        Backs the "what did you just do / what were you thinking" tool.
        """
        limit = self._clamp_int(limit, 5, 1, 20)
        try:
            rows = self.fetch_data(
                "SELECT tool_name, arguments, result, reasoning, timestamp "
                "FROM tool_usage WHERE user_id = ? AND IFNULL(group_id,'') = ? "
                "ORDER BY id DESC LIMIT ?",
                (user_id, group_id or "", limit),
            )
        except sqlite3.Error:
            logger.exception("tool chain query failed")
            return []
        return [
            {"tool_name": r[0], "arguments": r[1], "result": r[2],
             "reasoning": r[3], "timestamp": r[4]}
            for r in reversed(rows)
        ]

    def get_feature_requests(self, status: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """Feature requests, newest first (optionally filtered by status)."""
        limit = self._clamp_int(limit, 20, 1, 100)
        sql = ("SELECT id, summary_or_request, category, priority, status, timestamp "
               "FROM (SELECT id, COALESCE(NULLIF(ai_summary,''), request_text) AS summary_or_request, "
               "             category, priority, status, timestamp FROM feature_requests)")
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        try:
            rows = self.fetch_data(sql, params)
        except sqlite3.Error:
            logger.exception("feature request query failed")
            return []
        return [
            {"id": r[0], "summary": r[1], "category": r[2],
             "priority": r[3], "status": r[4], "timestamp": r[5]}
            for r in rows
        ]

    def get_tool_stats(self) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT tool_name, COUNT(*) as cnt FROM tool_usage GROUP BY tool_name ORDER BY cnt DESC"
        )

    def get_total_stats(self) -> dict[str, Any]:
        try:
            msgs = self.fetch_data("SELECT COUNT(*) FROM group_messages")[0][0]
        except Exception:
            msgs = 0
        try:
            chats = self.fetch_data("SELECT COUNT(*) FROM history")[0][0]
        except Exception:
            chats = 0
        try:
            tarots = self.fetch_data("SELECT COUNT(*) FROM tarot_history")[0][0]
        except Exception:
            tarots = 0
        try:
            profiles = self.fetch_data("SELECT COUNT(*) FROM user_profiles")[0][0]
        except Exception:
            profiles = 0
        try:
            stickers = self.fetch_data("SELECT COUNT(*) FROM tarot_content")[0][0]
        except Exception:
            stickers = 0
        return {
            "group_messages": msgs,
            "chat_turns": chats,
            "tarot_draws": tarots,
            "user_profiles": profiles,
            "tarot_cards": stickers,
        }

    # ── Generic key/value state ────────────────────────────
    def get_state(self, key: str, default: str = "") -> str:
        try:
            rows = self.fetch_data("SELECT value FROM app_state WHERE key = ?", (key,))
        except sqlite3.Error:
            return default
        return rows[0][0] if rows else default

    def set_state(self, key: str, value: str) -> None:
        try:
            self.execute_action(
                "INSERT INTO app_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        except sqlite3.Error:
            logger.exception("app_state write failed for %s", key)

    # ── Amp heads (箱头库) ─────────────────────────────────    # `source` records where a row came from: 'manual' (the curated file) or
    # 'wikipedia' (crawled). It matters because the two have very different
    # reliability, and the push says which one you are looking at.
    AMP_HEAD_FIELDS = (
        "brand", "model", "year", "origin", "kind", "power", "tubes",
        "tone", "price", "famous", "tip", "source", "source_url", "fetched_at",
    )

    def seed_amp_heads(self, connect: sqlite3.Connection | None = None) -> int:
        """Load the curated amp-head list, idempotently.

        Keyed on (brand, model), so restarting after editing
        ``amp_heads_data.py`` *updates* the row rather than duplicating it.
        That is the intended way to fix a wrong year or a stale price.

        The curated file always wins: a hand-written row overwrites anything a
        crawl produced for the same amp, and is re-marked as 'manual' so the
        push stops attributing it to Wikipedia.
        """
        try:
            from amp_heads_data import AMP_HEADS, COLUMNS
        except ImportError:
            logger.exception("amp_heads dataset unavailable")
            return 0

        placeholders = ", ".join("?" for _ in COLUMNS)
        updates = ", ".join(
            f"{c}=excluded.{c}" for c in COLUMNS if c not in ("brand", "model")
        )
        sql = (
            f"INSERT INTO amp_heads ({', '.join(COLUMNS)}, source) "
            f"VALUES ({placeholders}, 'manual') "
            f"ON CONFLICT(brand, model) DO UPDATE SET {updates}, source='manual'"
        )
        own = connect is None
        if own:
            connect = self.get_connect()
        try:
            connect.executemany(sql, AMP_HEADS)
            if own:
                connect.commit()
            return len(AMP_HEADS)
        except sqlite3.Error:
            logger.exception("amp_heads seeding failed")
            return 0

    def count_amp_heads(self, source: str = "") -> int:
        sql = "SELECT COUNT(*) FROM amp_heads"
        params: tuple[Any, ...] = ()
        if source:
            sql += " WHERE source = ?"
            params = (source,)
        try:
            return int(self.fetch_data(sql, params)[0][0])
        except (sqlite3.Error, IndexError, TypeError, ValueError):
            return 0

    def amp_head_exists(self, brand: str, model: str) -> bool:
        try:
            rows = self.fetch_data(
                "SELECT 1 FROM amp_heads WHERE brand = ? AND model = ? LIMIT 1",
                (brand, model),
            )
        except sqlite3.Error:
            return False
        return bool(rows)

    def add_amp_head(self, data: dict[str, Any], source: str,
                     source_url: str = "") -> bool:
        """Insert one row (crawl path). Returns False if it already exists.

        Deliberately does *not* update on conflict: a crawl must never clobber
        a curated row or overwrite a previous crawl's better text.
        """
        cols = [c for c in self.AMP_HEAD_FIELDS if c not in ("source", "source_url",
                                                             "fetched_at")]
        values = [str(data.get(c) or "").strip() for c in cols]
        if not values[0] or not values[1]:
            return False
        if self.amp_head_exists(values[0], values[1]):
            return False
        cols += ["source", "source_url", "fetched_at"]
        values += [source, source_url, datetime.now().strftime("%Y-%m-%d %H:%M:%S")]
        placeholders = ", ".join("?" for _ in cols)
        try:
            self.execute_action(
                f"INSERT INTO amp_heads ({', '.join(cols)}) VALUES ({placeholders})",
                tuple(values),
            )
        except sqlite3.Error:
            logger.exception("amp_head insert failed")
            return False
        return True

    def get_amp_heads(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        """The whole library for the dashboard, newest/manual first."""
        limit = self._clamp_int(limit, 200, 1, 500)
        offset = self._clamp_int(offset, 0, 0, 100000)
        fields = ", ".join(["id"] + list(self.AMP_HEAD_FIELDS))
        try:
            rows = self.fetch_data(
                f"SELECT {fields} FROM amp_heads "
                "ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END, brand, model "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            )
        except sqlite3.Error:
            logger.exception("amp_heads query failed")
            return []
        keys = ["id"] + list(self.AMP_HEAD_FIELDS)
        return [dict(zip(keys, r)) for r in rows]

    def delete_amp_head(self, head_id: int) -> None:
        self.execute_action("DELETE FROM amp_heads WHERE id = ?", (head_id,))

    def get_amp_head_of_the_day(self) -> dict[str, Any] | None:
        """Today's amp head, rotating through the whole library once per cycle.

        Deliberately *not* ``ORDER BY RANDOM()``: with a daily push that
        repeats the same amp back-to-back far too easily, which reads as a
        bug. Walking the list by day-of-year guarantees every entry is shown
        before any repeats, with no per-group bookkeeping. Side effect worth
        keeping: every group gets the same amp on the same day, so people can
        actually talk about it.
        """
        fields = ", ".join(self.AMP_HEAD_FIELDS)
        sql = (
            f"SELECT {fields} FROM amp_heads ORDER BY id LIMIT 1 OFFSET ("
            "  CAST(strftime('%j','now','localtime') AS INTEGER) "
            "  % MAX((SELECT COUNT(*) FROM amp_heads), 1))"
        )
        try:
            rows = self.fetch_data(sql)
        except sqlite3.Error:
            logger.exception("amp_head query failed")
            return None
        if not rows:
            return None
        return dict(zip(self.AMP_HEAD_FIELDS, rows[0]))

    def get_all_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, group_id, role, substr(content,1,200), tool_calls, "
            "       timestamp, reasoning "
            "FROM history ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {"user_id": r[0], "group_id": r[1], "role": r[2],
             "content": r[3], "has_tools": bool(r[4]), "time": r[5],
             "tool_calls": r[4] or "", "reasoning": r[6] or ""}
            for r in rows
        ]

    def get_all_tarot_history(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, card_name, timestamp FROM tarot_history ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [{"user_id": r[0], "card": r[1], "time": r[2]} for r in rows]

    def get_all_profiles(self) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, group_id, user_name, profile_json, message_count, last_updated FROM user_profiles ORDER BY message_count DESC"
        )
        import json
        result = []
        for r in rows:
            try:
                p = json.loads(r[3])
            except (json.JSONDecodeError, TypeError):
                p = {}
            result.append({
                "user_id": r[0], "group_id": r[1], "user_name": r[2],
                "profile": p, "msg_count": r[4], "updated": r[5],
            })
        return result

    # ── Stickers ──────────────────────────────────────

    def insert_sticker(self, filename: str, file_hash: str, file_size: int,
                       source_group_id: str = "", source_user_id: str = "") -> None:
        self.execute_action(
            "INSERT OR IGNORE INTO stickers (filename, file_hash, file_size, source_group_id, source_user_id) VALUES (?, ?, ?, ?, ?)",
            (filename, file_hash, file_size, source_group_id or None, source_user_id or None),
        )

    def update_sticker_category(self, filename: str, category: str, content_desc: str = "", emotion: str = "") -> None:
        self.execute_action(
            "UPDATE stickers SET category=?, content_desc=?, emotion=?, categorized_at=datetime('now','localtime') WHERE filename=?",
            (category, content_desc, emotion, filename),
        )

    def get_stickers(self, category: str = "") -> list[dict[str, Any]]:
        if category:
            rows = self.fetch_data(
                "SELECT filename, file_hash, category, content_desc, emotion, file_size, collected_at FROM stickers WHERE category=? ORDER BY collected_at DESC",
                (category,),
            )
        else:
            rows = self.fetch_data(
                "SELECT filename, file_hash, category, content_desc, emotion, file_size, collected_at FROM stickers ORDER BY collected_at DESC"
            )
        return [{"filename": r[0], "file_hash": r[1], "category": r[2],
                 "content_desc": r[3], "emotion": r[4], "file_size": r[5],
                 "collected_at": r[6]} for r in rows]

    def get_uncategorized_stickers(self) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT filename, file_hash FROM stickers WHERE category='未分类' OR category='' ORDER BY collected_at DESC"
        )

    def count_stickers_by_category(self) -> list[tuple[Any, ...]]:
        return self.fetch_data(
            "SELECT category, COUNT(*) FROM stickers GROUP BY category ORDER BY COUNT(*) DESC"
        )

    def get_group_profiles(self, group_id: str) -> list[dict[str, Any]]:
        rows = self.fetch_data(
            "SELECT user_id, user_name, profile_json, message_count FROM user_profiles "
            "WHERE group_id = ? ORDER BY message_count DESC LIMIT 20",
            (group_id,),
        )
        import json
        profiles = []
        for user_id, user_name, pj, cnt in rows:
            try:
                p = json.loads(pj)
            except (json.JSONDecodeError, TypeError):
                p = {}
            # Resolve current user name from group_messages (handles nick changes)
            latest_name = self.get_latest_user_name(user_id, group_id)
            profiles.append({
                "user_id": user_id,
                "user_name": latest_name or user_name,
                "profile": p,
                "message_count": cnt,
            })
        return profiles

    def cleanup_orphan_stickers(self, sticker_dir: str) -> int:
        """Remove DB entries for stickers whose files no longer exist on disk.
        Returns number of removed records."""
        import os as _os
        try:
            rows = self.fetch_data("SELECT filename FROM stickers")
            disk_files = set(_os.listdir(sticker_dir))
            orphans = [r[0] for r in rows if r[0] not in disk_files]
            for fname in orphans:
                self.execute_action("DELETE FROM stickers WHERE filename = ?", (fname,))
            if orphans:
                logger.info("Cleaned %d orphan sticker DB records", len(orphans))
            return len(orphans)
        except Exception:
            logger.exception("Failed to clean orphan stickers")
            return 0

    # ── Group purge ─────────────────────────────────────
    # Tables with a real group_id column: everything here is scoped to one
    # group and can be deleted outright.
    _GROUP_SCOPED: tuple[tuple[str, str], ...] = (
        ("group_messages", "群消息"),
        ("history", "对话记录"),
        ("reminders", "提醒"),
        ("tool_usage", "工具调用"),
        ("user_profiles", "用户画像"),
        ("user_affection", "好感度"),
        ("user_affection_log", "好感度流水"),
        ("feature_requests", "功能需求"),
    )

    def _group_user_ids(self, group_id: str) -> list[str]:
        """Distinct users that ever spoke in this group."""
        try:
            return [r[0] for r in self.fetch_data(
                "SELECT DISTINCT user_id FROM group_messages WHERE group_id = ?",
                (str(group_id),),
            )]
        except sqlite3.Error:
            return []

    def group_purge_preview(self, group_id: str) -> dict[str, int]:
        """Row counts purge_group() would delete, for the confirm dialog.

        `users` / `learning_log` / `user_settings` are per-user artifacts that
        are NOT group-scoped in the schema (a user can be in several groups),
        so they are reported separately and clearly.
        """
        gid = str(group_id)
        counts: dict[str, int] = {}
        for table, label in self._GROUP_SCOPED:
            try:
                counts[table] = self.fetch_data(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id = ?", (gid,)
                )[0][0]
            except (sqlite3.Error, IndexError):
                counts[table] = 0
        try:
            counts["group_settings"] = self.fetch_data(
                "SELECT COUNT(*) FROM feature_settings WHERE scope_type='group' AND scope_id = ?",
                (gid,),
            )[0][0]
        except (sqlite3.Error, IndexError):
            counts["group_settings"] = 0

        users = self._group_user_ids(gid)
        counts["users"] = len(users)
        counts["learning_log"] = 0
        counts["user_settings"] = 0
        if users:
            marks = ",".join("?" * len(users))
            try:
                counts["learning_log"] = self.fetch_data(
                    f"SELECT COUNT(*) FROM learning_log WHERE user_id IN ({marks})",
                    tuple(users),
                )[0][0]
            except (sqlite3.Error, IndexError):
                pass
            try:
                counts["user_settings"] = self.fetch_data(
                    f"SELECT COUNT(*) FROM feature_settings "
                    f"WHERE scope_type='user' AND scope_id IN ({marks})",
                    tuple(users),
                )[0][0]
            except (sqlite3.Error, IndexError):
                pass
        return counts

    def purge_group(self, group_id: str) -> dict[str, int]:
        """Delete everything belonging to a group.

        Covers all group-scoped tables plus the per-user artifacts of members
        seen in this group (profiles/affection are group-scoped already;
        learning notes and per-user toggles are not). Sticker files are a
        global gallery and are intentionally left alone.
        """
        gid = str(group_id)
        deleted: dict[str, int] = {}
        users = self._group_user_ids(gid)

        for table, label in self._GROUP_SCOPED:
            try:
                n = self.fetch_data(
                    f"SELECT COUNT(*) FROM {table} WHERE group_id = ?", (gid,)
                )[0][0]
                self.execute_action(f"DELETE FROM {table} WHERE group_id = ?", (gid,))
                deleted[table] = n
            except (sqlite3.Error, IndexError):
                logger.exception("purge_group: failed to clear %s", table)

        try:
            self.execute_action(
                "DELETE FROM feature_settings WHERE scope_type='group' AND scope_id = ?",
                (gid,),
            )
        except sqlite3.Error:
            logger.exception("purge_group: failed to clear group feature settings")

        if users:
            marks = ",".join("?" * len(users))
            for sql, params, key in (
                (f"DELETE FROM learning_log WHERE user_id IN ({marks})", tuple(users), "learning_log"),
                (f"DELETE FROM feature_settings WHERE scope_type='user' AND scope_id IN ({marks})",
                 tuple(users), "user_settings"),
            ):
                try:
                    self.execute_action(sql, params)
                    deleted[key] = len(users)
                except sqlite3.Error:
                    logger.exception("purge_group: failed to clear %s", key)

        self._member_cache.pop(gid, None)
        logger.info("Purged group %s: %s", gid, deleted)
        return deleted
