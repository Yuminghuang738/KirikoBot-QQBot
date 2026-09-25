"""The bot's own message log (recall), and tool/feature-gate wiring."""
from __future__ import annotations


class TestBotMessageLog:
    """The bot's own messages: needed for recall and quote resolution.

    Message ids are the platform's strings; the TEXT affinity means even a
    numeric id comes back as its text form.
    """

    def test_records_and_finds_the_last_message(self, db):
        db.record_bot_message("g1", "555", "我是机器人")
        last = db.get_last_bot_message("g1")
        assert last["message_id"] == "555"
        assert "机器人" in last["text"]

    def test_returns_none_when_nothing_sent(self, db):
        assert db.get_last_bot_message("g1") is None

    def test_recalled_messages_are_not_returned_again(self, db):
        db.record_bot_message("g1", "555", "撤回我")
        db.mark_bot_message_recalled("555")
        assert db.get_last_bot_message("g1") is None

    def test_only_the_most_recent_is_returned(self, db):
        db.record_bot_message("g1", "1", "第一条")
        db.record_bot_message("g1", "2", "第二条")
        assert db.get_last_bot_message("g1")["message_id"] == "2"

    def test_messages_are_scoped_per_group(self, db):
        db.record_bot_message("g1", "1", "给 g1")
        db.record_bot_message("g2", "2", "给 g2")
        assert db.get_last_bot_message("g1")["message_id"] == "1"

    def test_old_messages_fall_outside_the_recall_window(self, db):
        """QQ loses recall ability after ~2 minutes, so stale ids must not be offered."""
        db.execute_action(
            "INSERT INTO bot_messages (group_id, message_id, text, created_at) "
            "VALUES ('g1', '999', '很久以前', datetime('now','localtime','-10 minutes'))"
        )
        assert db.get_last_bot_message("g1", max_age_seconds=110) is None
        assert db.get_last_bot_message("g1", max_age_seconds=3600)["message_id"] == "999"

    def test_none_message_id_is_ignored(self, db):
        db.record_bot_message("g1", None, "发送失败没有 id")
        assert db.get_last_bot_message("g1") is None


class TestToolRegistration:
    TOOLS = {"recall_message"}

    def test_schemas_are_exposed_to_the_model(self):
        from ai_tools_list import AiTools

        names = {t["function"]["name"] for t in AiTools().ai_tools()}
        assert self.TOOLS <= names

    def test_feature_gate_knows_them(self):
        from feature_gate import FEATURE_KEYS, TOOL_FEATURE

        assert "recall" in FEATURE_KEYS
        assert TOOL_FEATURE["recall_message"] == "recall"

    def test_disabling_a_feature_hides_its_tool(self):
        from feature_gate import disabled_tool_names

        blocked = disabled_tool_names({"recall"})
        assert "recall_message" in blocked


class TestTurnChainRecording:
    """The chain belongs to a turn, and users ask about the PREVIOUS reply."""

    def test_saves_and_reads_back_the_thinking_chain(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "今天多云", "", "",
                                reasoning="先查天气再回答")
        last = db.get_last_bot_turn("u1", "g1")
        assert last["content"] == "今天多云"
        assert last["reasoning"] == "先查天气再回答"

    def test_stores_the_tool_chain_json(self, db):
        chain = '[{"name": "weather", "arguments": "{\\"city\\": \\"杭州\\"}"}]'
        db.deposit_chat_history("assistant", "u1", "g1", "查到了", chain, "", "")
        last = db.get_last_bot_turn("u1", "g1")
        assert '"weather"' in last["tool_calls"]

    def test_returns_the_most_recent_reply_only(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "第一句", "", "", "")
        db.deposit_chat_history("user", "u1", "g1", "第二问", "", "", "")
        db.deposit_chat_history("assistant", "u1", "g1", "第二句", "", "", "")
        assert db.get_last_bot_turn("u1", "g1")["content"] == "第二句"

    def test_user_rows_are_not_returned(self, db):
        db.deposit_chat_history("user", "u1", "g1", "用户的问话", "", "", "")
        assert db.get_last_bot_turn("u1", "g1") is None

    def test_scoped_per_user_and_group(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "给 u1 的", "", "", "")
        db.deposit_chat_history("assistant", "u2", "g1", "给 u2 的", "", "", "")
        assert db.get_last_bot_turn("u2", "g1")["content"] == "给 u2 的"
        assert db.get_last_bot_turn("u1", "g2") is None

    def test_history_page_exposes_chain_and_reasoning(self, db):
        db.deposit_chat_history("assistant", "u1", "g1", "回复",
                                '[{"name":"dice"}]', "", reasoning="想了想")
        rec = db.get_all_history(10)[0]
        assert rec["tool_calls"] == '[{"name":"dice"}]'
        assert rec["reasoning"] == "想了想"

    def test_reasoning_column_is_migrated_onto_old_dbs(self, tmp_path):
        import sqlite3

        from database_manager import DatabaseManager

        path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL,
                group_id TEXT, role TEXT NOT NULL, content TEXT NOT NULL,
                tool_calls TEXT, tool_call_id TEXT,
                timestamp DATETIME DEFAULT (datetime('now','localtime'))
            )"""
        )
        conn.execute("INSERT INTO history (user_id,role,content) VALUES ('u1','assistant','旧回复')")
        conn.commit()
        conn.close()

        DatabaseManager(path)
        conn = sqlite3.connect(path)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(history)")}
            assert "reasoning" in cols
            assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 1
        finally:
            conn.close()


class TestExplainSelfRawDump:
    """explain_self is a DEBUG tool: it must show the chain, not re-tell it.

    An earlier version handed the record back to the model and asked it to
    narrate in first person, so the user got a paraphrase of the thinking
    chain instead of the chain itself. Since the whole point is inspecting
    what the model actually thought, the dump is now reproduced verbatim.
    """

    RAW = "第一步：先看用户问了什么\n第二步：翻一下群里的记录\n  这里缩进也要保留\n第三步：决定回答"

    class _Incoming:
        message_id = 42

    class _Robot:
        msg_type, group_id, user_id, user_name = "group", "g1", "u1", "小明"

        def __init__(self, llbot):
            self.client = llbot
            self.incoming = TestExplainSelfRawDump._Incoming()

        def send(self, message):
            # 和真实 RobotServer 一样：工具只管给段列表，原消息由这里带上
            # （官方平台没带 msg_id 的发送一律 400）
            return self.client.send(self.incoming, message)

    class _AI:
        def __init__(self):
            self.ai_message = {
                "tool_calls": [{"id": "call_1", "function": {"name": "explain_self"}}],
            }
            self.tool_result_text = ""
            self.user_text = ""

    class _LLBot:
        def __init__(self):
            self.sent = []

        def send(self, msg, message):
            self.sent.append((getattr(msg, "group_id", "g1"), message))
            return True

    def _run(self, db, reasoning=RAW, chain="", content="回复内容"):
        from ai_tools import ExplainSelfTool

        db.deposit_chat_history("assistant", "u1", "g1", content, chain, "",
                                reasoning=reasoning)
        llbot = self._LLBot()
        ai = self._AI()
        ExplainSelfTool(db).explain_self_call(self._Robot(llbot), ai)
        text = "\n".join(
            seg["data"]["text"]
            for _, segments in llbot.sent
            for seg in segments
            if seg["type"] == "text"
        )
        return llbot.sent, text, ai

    def test_reasoning_is_reproduced_verbatim(self, db):
        """Newlines and indentation survive — this is the raw text, not a summary."""
        _, text, _ = self._run(db)
        assert self.RAW in text

    def test_whitespace_is_not_collapsed(self, db):
        """" ".join(split()) would have flattened the chain into one line."""
        _, text, _ = self._run(db)
        assert "翻一下群里的记录\n  这里缩进也要保留" in text

    def test_dump_is_sent_directly_not_left_to_the_model(self, db):
        sent, _, ai = self._run(db)
        assert sent, "the dump must actually be sent to the chat"
        assert "思维链原文" in sent[0][1][-1]["data"]["text"]

    def test_model_is_told_to_stay_silent(self, db):
        """Otherwise the follow-up turn paraphrases what we just dumped."""
        _, _, ai = self._run(db)
        assert "不要再说" in ai.tool_result_text

    def test_tool_chain_and_reply_are_included(self, db):
        chain = '[{"name": "dice", "arguments": "{\\"n\\": 6}"}]'
        _, text, _ = self._run(db, chain=chain, content="我掷了个 6")
        assert "dice" in text
        assert "我掷了个 6" in text

    def test_missing_reasoning_says_so_instead_of_inventing(self, db):
        _, text, _ = self._run(db, reasoning="")
        assert "没有思维链" in text

    def test_no_previous_turn_is_reported_plainly(self, db):
        from ai_tools import ExplainSelfTool

        llbot = self._LLBot()
        ExplainSelfTool(db).explain_self_call(self._Robot(llbot), self._AI())
        assert "没有上一轮" in llbot.sent[0][1][-1]["data"]["text"]

    def test_long_dumps_are_chunked_not_truncated(self, db):
        """QQ text limits are per-message, so split rather than lose the tail.

        A thinking chain is often one unbroken paragraph, so this uses no
        newlines at all — line-based chunking alone would emit one huge
        segment and the tail would be dropped by the platform.
        """
        sent, text, _ = self._run(db, reasoning="x" * 5000)
        assert sent, "nothing was sent"
        texts = [
            seg["data"]["text"]
            for _, segments in sent
            for seg in segments
            if seg["type"] == "text"
        ]
        assert all(len(t) <= 1300 for t in texts), "chunk over QQ's limit"
        assert text.count("x") == 5000, "the tail must survive chunking"
        assert "(1/" in text and "5/" in text

    def test_runs_without_a_follow_up_turn(self):
        """Registered self-contained, so the model never re-narrates the dump."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "KirikoBot", "main.py",
        )
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if "SELF_CONTAINED_TOOLS" in targets:
                    names = {e.value for e in node.value.elts}
                    assert "explain_self" in names
                    return
        raise AssertionError("SELF_CONTAINED_TOOLS not found")
