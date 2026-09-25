from __future__ import annotations

import json
import logging
import os
import random
from typing import Any

from config import Config

logger = logging.getLogger(__name__)


# ── Helper ──────────────────────────────────────────────

def _set_tool_meta(ai_server: Any, tool_calls: Any, extra: str = "") -> None:
    if tool_calls:
        ai_server.airesponse_tool_id = tool_calls[0].get("id", "")
        ai_server.airesponse_tool_calls = tool_calls


# ══════════════════════════════════════════════════════════
#  Tarot
# ══════════════════════════════════════════════════════════

class Tarot:
    def __init__(self, database_manager: Any) -> None:
        self.database_manager = database_manager

    def _draw_card(self) -> dict[str, str]:
        try:
            data = self.database_manager.takeout("tarot_content", "card_name, card_text, card_path")
        except Exception:
            logger.exception("tarot_content query failed")
            return {"card_name": "未知", "card_text": "数据库不可用", "card_path": ""}
        if not data:
            return {"card_name": "未知", "card_text": "牌库为空", "card_path": ""}
        card_name, card_text, card_path = random.choice(data)
        return {"card_name": card_name, "card_text": card_text, "card_path": card_path}

    def _lookup_target(self, robot: Any, target_name: str) -> str | None:
        """Find a group member's user_id by name. Returns user_id or None."""
        try:
            rows = self.database_manager.fetch_data(
                "SELECT DISTINCT user_id FROM group_messages "
                "WHERE group_id=? AND user_name LIKE ? ORDER BY id DESC LIMIT 1",
                (robot.group_id, f"%{target_name}%"),
            )
            return rows[0][0] if rows else None
        except Exception:
            return None

    def _resend_today(self, robot: Any, ai: Any, card: dict[str, Any],
                      display_name: str, is_for_self: bool) -> None:
        """Tell them they already drew today, and show that same card again."""
        from qq_official import MessageBuilder

        builder = MessageBuilder()
        if not is_for_self:
            builder.text(f"🔮 {display_name}的牌今天已经抽过了。\n\n")
        if card.get("card_path"):
            builder.image(card["card_path"])
        builder.text(f"\n🎴 {display_name}今天抽到的还是这张：{card['card_name']}")
        if card.get("card_text"):
            builder.text(f"\n{card['card_text']}")
        if robot.msg_type == "group":
            robot.client.send_group_msg(robot.group_id or "", builder.build())
        else:
            robot.client.send_private_msg(robot.user_id, builder.build())

        ai.model_type = Config.DEEPSEEK_MODEL
        ai.thinking_type = "disabled"
        from prompt_builder import build_role_prompt
        ai.system_text = build_role_prompt(Config.TAROT_ROLE)
        ai.user_text = (
            f"{display_name}今天已经抽过牌了，抽到的是「{card['card_name']}」，"
            f"牌面：{card.get('card_text') or ''}。"
            "牌已经发出去了。请告诉对方今天只能抽一次，一天一张，"
            "并且用这句话把这张牌再解读一遍——不管这牌是好是坏，抽到什么就是什么，"
            "不要因为对方想要别的结果就重抽或者改口。"
        )
        ai.ai_request()
        if ai.ai_text:
            reply = MessageBuilder().text(ai.ai_text.strip())
            if robot.msg_type == "group":
                robot.client.send_group_msg(robot.group_id or "", reply.build())
            else:
                robot.client.send_private_msg(robot.user_id, reply.build())

    def tarot_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        # Parse target_name from tool arguments
        target_name = ""
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
                target_name = args.get("target_name", "")
            except (json.JSONDecodeError, TypeError):
                logger.debug("ai_tools.tarot_call 忽略了异常", exc_info=True)

        # Determine who the card is for
        is_for_self = not target_name or target_name == robot.user_name
        display_name = robot.user_name if is_for_self else target_name
        target_uid = self._lookup_target(robot, target_name) if not is_for_self else None

        # One card per person per day. The limit is on the REQUESTER, which is
        # also who tarot_history records, so asking on behalf of ten friends
        # doesn't get you ten draws. A repeat re-serves the original card
        # instead of drawing a new one — the point is that it stands, whether
        # it was good or bad.
        try:
            already = self.database_manager.get_today_tarot(robot.user_id)
        except Exception:
            logger.debug("tarot daily check failed", exc_info=True)
            already = None
        if already:
            self._resend_today(robot, ai, already, display_name, is_for_self)
            return

        card = self._draw_card()

        # Send card image + name
        from qq_official import MessageBuilder
        builder = MessageBuilder()
        if not is_for_self and target_name:
            builder.text(f"🔮 应 {robot.user_name} 的要求，给 {target_name} 抽了一张塔罗牌！\n\n")
        if card["card_path"]:
            builder.image(card["card_path"])
        builder.text(f"\n🎴 {display_name}的塔罗牌：{card['card_name']}\n{card['card_text']}")
        if robot.msg_type == "group":
            robot.client.send_group_msg(robot.group_id or "", builder.build())
        else:
            robot.client.send_private_msg(robot.user_id, builder.build())

        # AI interpretation
        ai.model_type = Config.DEEPSEEK_MODEL
        ai.thinking_type = "disabled"
        from prompt_builder import build_role_prompt
        ai.system_text = build_role_prompt(Config.TAROT_ROLE)
        ai.user_text = (
            f"抽牌人：{display_name}，抽牌结果：{card['card_name']}，牌面：{card['card_text']}。"
            f"请为{display_name}解读这张牌。"
        )
        ai.ai_request()

        if ai.ai_text:
            # @ the target person in the reply
            reply_builder = MessageBuilder()
            if not is_for_self and target_name:
                reply_builder.text(f"@{target_name} ")
            reply_builder.text(ai.ai_text.strip())
            if robot.msg_type == "group":
                robot.client.send_group_msg(robot.group_id or "", reply_builder.build())
            else:
                robot.client.send_private_msg(robot.user_id, reply_builder.build())

        # Deposit history for the REQUESTER (not target)
        try:
            self.database_manager.deposit_tarot_history(robot.user_id, card["card_name"])
            self.database_manager.deposit_chat_history("user", robot.user_id, robot.group_id, robot.msg, "", "")
            self.database_manager.deposit_chat_history("assistant", robot.user_id, robot.group_id, ai.ai_text, json.dumps(ai.airesponse_tool_calls), "")
            self.database_manager.deposit_chat_history("tool", robot.user_id, robot.group_id, ai.user_text, "", ai.airesponse_tool_id)
        except Exception:
            logger.exception("Failed to deposit tarot history")


class Tarot_History:
    def __init__(self, database_manager: Any) -> None:
        self.database_manager = database_manager

    def tarot_history_call(self, robot: Any, ai: Any) -> None:
        try:
            rows = self.database_manager.takeout_tarot_history(robot.user_id)
        except Exception:
            logger.exception("tarot_history query failed")
            robot.reply("抱歉，获取塔罗牌记录时出了点问题~")
            return

        if not rows:
            robot.reply("你还没有抽取过塔罗牌哦～快来抽一张吧！(◕‿◕✿)")
        else:
            lines = ["你的塔罗牌记录："]
            for name, ts in rows:
                lines.append(f"  {ts} · {name}")
            robot.reply("\n".join(lines))

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)
        ai.user_text = robot.text if hasattr(robot, 'text') else ""


# ══════════════════════════════════════════════════════════
#  Gaming News
# ══════════════════════════════════════════════════════════

class GamingNews:
    def __init__(self, crawler: Any) -> None:
        self.crawler = crawler

    def gaming_news_call(self, robot: Any, ai: Any) -> None:
        try:
            items = self.crawler.fetch_gaming_news()
        except Exception:
            logger.exception("News crawl failed")
            items = []

        if not items:
            robot.reply("暂时没有获取到游戏新闻哦，请稍后再试~")
        else:
            lines = ["🎮 热点游戏新闻"]
            for i, n in enumerate(items, 1):
                lines.append(f"{i}. {n['title']}")
                meta = f"   🏷 {n['category']} | 🕐 {n['time']}"
                lines.append(meta)
                if n.get("summary"):
                    lines.append(f"   {n['summary'][:100]}")
            robot.reply("\n".join(lines))

        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = robot.text


# ══════════════════════════════════════════════════════════
#  Web Search (RAG-style: search → fetch → feed AI → reply)
# ══════════════════════════════════════════════════════════

class WebSearchTool:
    def __init__(self, web_search: Any) -> None:
        self.web_search = web_search

    def web_search_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        query = args.get("query", robot.msg)

        # Fetch search results + page content
        try:
            content = self.web_search.search_and_fetch(query)
        except Exception:
            logger.exception("Search+fetch failed")
            robot.reply("抱歉，联网搜索暂时不可用，请稍后再试~")
            return

        if not content:
            robot.reply("抱歉，没有搜索到相关内容呢～换个关键词试试吧 (｡•́︿•̀｡)")
            return

        # Feed search results to AI for synthesis
        from datetime import datetime
        current_date = datetime.now().strftime("%Y年%m月%d日")
        ai.model_type = Config.DEEPSEEK_MODEL
        ai.thinking_type = "enabled"
        ai.system_text = (
            "你是Kiriko，请根据以下搜索结果回答用户问题。"
            f"当前日期是{current_date}，你的知识截止于2025年，请以当前日期和搜索结果为准。"
            "用可爱的语气，简洁明了，不要长篇大论，控制在300字以内。"
            "禁止使用表格，用自然的段落文字回复。"
            f"\n\n用户问题：{robot.msg}\n\n搜索结果：\n{content}"
        )
        # Clear history for search context (irrelevant old messages confuse)
        ai.history_list = []
        ai.user_text = f"请根据以上搜索结果回答：{robot.msg}"
        ai.ai_request()

        if ai.ai_text:
            robot.reply(ai.ai_text)
        else:
            robot.reply("抱歉，没能整理出搜索结果，请换个问法试试~")

        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"搜索查询: {query}\n搜索内容: {content[:500]}"


# ══════════════════════════════════════════════════════════
#  Weather
# ══════════════════════════════════════════════════════════

class WeatherTool:
    def __init__(self, weather_service: Any) -> None:
        self.weather_service = weather_service

    def weather_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return
        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        city = args.get("city", "北京")

        data = self.weather_service.get_weather(city)
        if not data:
            ai.tool_result_text = f"未查到「{city}」的天气信息"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        lines = [
            f"城市：{data['city']}",
            f"温度：{data['temp']}°C（体感{data['feels_like']}°C），{data['weather_desc']}",
            f"湿度：{data['humidity']}%，风向：{data['wind_dir']}，风速：{data['wind_speed']}km/h",
            "未来预报：",
        ]
        for d in data["forecast"]:
            lines.append(f"  {d['date']} {d['desc']} {d['low']}~{d['high']}°C")
        ai.tool_result_text = "\n".join(lines)

        _set_tool_meta(ai, tool_calls)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Sticker
# ══════════════════════════════════════════════════════════

class StickerTool:
    STICKER_DIR = "/app/stickers"

    def __init__(self) -> None:
        self._cache: list[str] = []

    def _scan(self) -> list[str]:
        if self._cache:
            return self._cache
        import os
        try:
            self._cache = [
                f for f in os.listdir(self.STICKER_DIR)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            ]
        except Exception:
            logger.exception("Sticker scan failed")
        return self._cache

    def _pick_by_category(self, category: str) -> str | None:
        """Query DB for stickers matching the given category. Returns filename or None."""
        try:
            from database_manager import DatabaseManager
            db = DatabaseManager()
            stickers = db.get_stickers(category)
            if stickers:
                return random.choice(stickers)["filename"]
        except Exception:
            logger.debug("ai_tools._pick_by_category 忽略了异常", exc_info=True)
        return None

    def sticker_call(self, robot: Any, ai: Any) -> None:
        import json
        tool_calls = ai.ai_message.get("tool_calls")
        args: dict[str, Any] = {}
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                logger.debug("ai_tools.sticker_call 忽略了异常", exc_info=True)
        category = (args.get("category") or "").strip()

        chosen: str | None = None

        # Try DB lookup by category first
        if category and category != "":
            chosen = self._pick_by_category(category)
            if chosen:
                logger.info("Sticker: matched category '%s' → %s", category, chosen)

        # Fall back to random file scan
        if not chosen:
            self._cache = []  # Clear cache to pick up newly collected stickers
            stickers = self._scan()
            if not stickers:
                robot.reply("暂时没有表情包哦～请添加一些 Kiriko 图片吧！(｡•́︿•̀｡)")
                return
            chosen = random.choice(stickers)

        # Send image directly, no reply wrapper
        from qq_official import MessageBuilder
        builder = MessageBuilder().image(f"{self.STICKER_DIR}/{chosen}")
        if robot.msg_type == "group":
            robot.client.send_group_msg(robot.group_id or "", builder.build())
        else:
            robot.client.send_private_msg(robot.user_id, builder.build())
        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"发送了表情包({category or '随机'}): {chosen}"


# ══════════════════════════════════════════════════════════
#  Hitokoto (一言)
# ══════════════════════════════════════════════════════════

class HitokotoTool:
    def __init__(self, service: Any) -> None:
        self.service = service

    def hitokoto_call(self, robot: Any, ai: Any) -> None:
        q = self.service.get_quote()
        if q and q["text"]:
            lines = [f"💬 {q['text']}"]
            if q["source"]:
                credit = f"—— {q['source']}"
                if q["author"]:
                    credit += f" ({q['author']})"
                lines.append(credit)
            robot.reply("\n".join(lines))
        else:
            robot.reply("呜～一言没抓到呢，再试一次吧！")
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = robot.text


# ══════════════════════════════════════════════════════════
#  Food Picker
# ══════════════════════════════════════════════════════════

class FoodPickerTool:
    FOODS = [
        "🍜 兰州拉面", "🍛 咖喱饭", "🍣 寿司", "🍕 披萨", "🌯 煎饼果子",
        "🥟 饺子", "🍔 汉堡", "🌮 塔可", "🍝 意面", "🥘 麻辣香锅",
        "🍱 便当", "🍲 火锅", "🥗 沙拉", "🍗 炸鸡", "🧋 奶茶配小吃",
        "🍜 酸辣粉", "🥟 小笼包", "🍚 盖浇饭", "🍖 烤肉", "🥘 煲仔饭",
        "🍜 重庆小面", "🍤 天妇罗", "🍙 饭团", "🥞 煎饼", "🍢 关东煮",
        "🥡 炒饭", "🍝 炒面", "🥓 烧烤", "🍕 馕坑肉", "🥘 黄焖鸡",
    ]
    EXTRAS = [
        "就决定是你啦～", "Kiriko也想吃这个！", "这个怎么样？",
        "今天试试这个吧～", "不错的选择呢 (◕‿◕✿)",
    ]

    def food_picker_call(self, robot: Any, ai: Any) -> None:
        food = random.choice(self.FOODS)
        extra = random.choice(self.EXTRAS)
        ai.tool_result_text = f"推荐食物：{food}，{extra}"
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Dice
# ══════════════════════════════════════════════════════════

class DiceTool:
    def dice_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        sides = 6
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
                sides = max(2, int(args.get("sides", 6)))
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.debug("ai_tools.dice_call 忽略了异常", exc_info=True)

        result = random.randint(1, sides)
        if sides == 6:
            emoji = {1: "⚀", 2: "⚁", 3: "⚂", 4: "⚃", 5: "⚄", 6: "⚅"}.get(result, "")
            ai.tool_result_text = f"D6骰子结果：{emoji} {result}点"
        elif sides == 20:
            tag = "大成功" if result == 20 else ("大失败" if result == 1 else "")
            ai.tool_result_text = f"D20骰子结果：{result}点{'，' + tag if tag else ''}"
        else:
            ai.tool_result_text = f"D{sides}骰子结果：{result}点"
        _set_tool_meta(ai, tool_calls)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Bilibili Trending
# ══════════════════════════════════════════════════════════

class BilibiliTool:
    def __init__(self, service: Any) -> None:
        self.service = service

    def bilibili_call(self, robot: Any, ai: Any) -> None:
        items = self.service.get_trending() or self.service.get_hot_videos()
        if not items:
            robot.reply("呜～B站热搜获取失败了，稍后再试吧 (｡•́︿•̀｡)")
            return
        lines = ["📺 B站热搜"]
        for i, item in enumerate(items, 1):
            lines.append(f"{i}. {item['keyword']}")
        robot.reply("\n".join(lines))
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = robot.text


# ══════════════════════════════════════════════════════════
#  Current Time
# ══════════════════════════════════════════════════════════

class TimeTool:
    def get_current_time_call(self, robot: Any, ai: Any) -> None:
        from datetime import datetime
        now = datetime.now()
        result = now.strftime("%Y-%m-%d %H:%M:%S")
        ai.ai_text = f"现在是 {result}"
        ai.tool_result_text = f"当前精确时间：{result}"
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Political News
# ══════════════════════════════════════════════════════════

class PoliticalNewsTool:
    def __init__(self, scraper: Any) -> None:
        self.scraper = scraper

    def political_news_call(self, robot: Any, ai: Any) -> None:
        try:
            items = self.scraper.fetch_all()
        except Exception:
            logger.exception("Political news fetch failed")
            items = []

        if not items:
            robot.reply("暂时没有获取到时政新闻哦，请稍后再试~")
            _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
            ai.user_text = robot.text
            return

        # Build raw news text for translation
        raw_lines = ["以下是最新国际时政新闻，请翻译成中文并美化排版："]
        for i, n in enumerate(items, 1):
            raw_lines.append(f"{i}. [{n['source']}] {n['title']}")
            if n.get("desc"):
                raw_lines.append(f"   摘要: {n['desc'][:200]}")
        raw_text = "\n".join(raw_lines)

        # Translate via flash model
        from ai_server import AiServer
        translator = AiServer(
            system_text=(
                "你是Kiriko的新闻翻译助手。将英文时政新闻翻译成中文，保持原意准确。"
                "排版要求：每条新闻用'📰 标题'开头，来源用括号标注，适当加入🔥💥🌍⚡🗳️等表情符号增强可读性。"
                "每条新闻之间空一行。不要编造或修改新闻事实，只做翻译和排版美化。"
                "禁止使用表格，每条新闻一小段，不超过3行。"
            ),
            user_text=raw_text,
            history_list=[],
            tools=[],
            model_type=Config.DEEPSEEK_MODEL,
            thinking_type="disabled",
        )
        translator.ai_request()

        if translator.ai_text:
            logger.info("News translation completed (%d chars)", len(translator.ai_text))
            robot.reply(translator.ai_text)
        else:
            # Fallback: show raw titles
            lines = ["📰 时政要闻"]
            for i, n in enumerate(items, 1):
                src = f" [{n['source']}]" if n.get("source") else ""
                lines.append(f"  {i}. {n['title']}{src}")
            robot.reply("\n".join(lines))

        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = robot.text


# ══════════════════════════════════════════════════════════
#  Balance Query
# ══════════════════════════════════════════════════════════

class BalanceTool:
    def __init__(self, service: Any) -> None:
        self.service = service

    def balance_call(self, robot: Any, ai: Any) -> None:
        try:
            result = self.service.format_balance()
        except Exception:
            logger.exception("Balance query failed")
            result = "查询DeepSeek余额失败"
        ai.tool_result_text = result
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Feature Request
# ══════════════════════════════════════════════════════════

class FeatureRequestTool:
    def __init__(self, db: Any) -> None:
        self.db = db

    def feature_request_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        request_text = args.get("request", "").strip()

        if not request_text:
            ai.tool_result_text = "功能请求内容为空"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        # Summarize and categorize via AI
        from ai_server import AiServer
        summarizer = AiServer(
            system_text=(
                "你是功能请求分析助手。根据用户的功能请求，输出JSON："
                '{"summary":"15字以内的功能名称","category":"新闻/游戏/AI对话/工具/通知/界面/其他",'
                '"priority":"high/medium/low"}。只输出JSON，不要其他内容。'
            ),
            user_text=f"功能请求：{request_text}",
            history_list=[],
            tools=[],
            model_type=Config.DEEPSEEK_MODEL,
            thinking_type="disabled",
        )
        summarizer.ai_request()
        raw = (summarizer.ai_text or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("\n", 1)[0]
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            meta = {"summary": request_text[:15], "category": "未分类", "priority": "medium"}

        summary = meta.get("summary", request_text[:15])
        category = meta.get("category", "未分类")
        priority = meta.get("priority", "medium")

        # Save to database
        try:
            self.db.deposit(
                "feature_requests",
                "(user_id, user_name, group_id, request_text, category, priority, status, ai_summary)",
                "(?, ?, ?, ?, ?, ?, 'pending', ?)",
                (robot.user_id, robot.user_name, robot.group_id, request_text, category, priority, summary),
            )
        except Exception:
            logger.exception("Failed to save feature request")
            ai.tool_result_text = "功能请求保存失败"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        ai.tool_result_text = (
            f"已记录功能请求：{summary}\n分类：{category} | 优先级：{priority}\n"
            f"感谢 {robot.user_name} 的建议！(◕‿◕✿)"
        )
        _set_tool_meta(ai, tool_calls)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Music Search & Playback (点歌)
# ══════════════════════════════════════════════════════════

class MusicTool:
    def __init__(self, music_service: Any) -> None:
        self.service = music_service
        self._recent_songs: dict[str, float] = {}  # dedup_key → timestamp
        self._recent_keywords: dict[str, str] = {}  # dedup_key → last keyword used

    def music_search_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        if not tool_calls:
            return

        try:
            args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        keyword = args.get("keyword", "").strip()

        if not keyword:
            keyword = robot.msg

        from qq_official import MessageBuilder
        import time as _time

        # Search for the best matching song
        song_info = self.service.search_best(keyword)

        if not song_info:
            robot.reply(f"抱歉，没有找到「{keyword}」的歌曲呢～换一首试试吧 (｡•́︿•̀｡)")
            _set_tool_meta(ai, tool_calls)
            ai.user_text = f"搜索歌曲: {keyword} - 未找到"
            return

        song_id = song_info.get("id", 0)
        artist = song_info.get("artist", "未知歌手")
        name = song_info.get("name", "未知歌曲")
        album = song_info.get("album", "")
        music_type = song_info.get("music_type", "163")

        now = _time.time()
        dedup_key = f"{robot.group_id or robot.user_id}:{song_id}"

        # ── Dedup: skip same song within 120 seconds per chat ──
        last_sent = self._recent_songs.get(dedup_key, 0)
        if now - last_sent < 120:
            logger.info(
                "Music dedup: skipping '%s - %s' (sent %.0fs ago to %s)",
                name, artist, now - last_sent, dedup_key,
            )
            # Try to find an alternative from search results
            alt_songs = self.service.search(keyword, limit=5)
            for alt in alt_songs:
                alt_id = alt.get("id", 0)
                alt_key = f"{robot.group_id or robot.user_id}:{alt_id}"
                if now - self._recent_songs.get(alt_key, 0) >= 120:
                    song_info = alt
                    song_id = alt_id
                    artist = alt.get("artist", "未知歌手")
                    name = alt.get("name", "未知歌曲")
                    album = alt.get("album", "")
                    music_type = alt.get("music_type", "163")
                    dedup_key = alt_key
                    logger.info("Music dedup: using alternative '%s - %s'", name, artist)
                    break
            else:
                # All alternatives also recently sent
                robot.reply(f"「{name} - {artist}」刚放过哦～等一会儿再点吧 (◕‿◕✿)")
                _set_tool_meta(ai, tool_calls)
                ai.user_text = f"已播放歌曲: {name} - {artist}（去重跳过，无可用替代）"
                return

        self._recent_songs[dedup_key] = now
        self._recent_keywords[dedup_key] = keyword

        # Clean up old entries (>5min)
        self._recent_songs = {
            k: v for k, v in self._recent_songs.items() if now - v < 300
        }
        self._recent_keywords = {
            k: v for k, v in self._recent_keywords.items() if k in self._recent_songs
        }

        # ── 1. Send song info text first ──
        info_lines = [f"🎵 {name}", f"👤 {artist}"]
        if album:
            info_lines.append(f"💿 {album}")
        info_lines.append("")

        # ── 2. Send music share card (QQ native music UI) ──
        info_lines.append(f"🎧 正在播放，点击收听 ↑")

        info_builder = MessageBuilder()
        info_builder.text("\n".join(info_lines))
        if robot.msg_type == "group":
            robot.client.send_group_msg(robot.group_id or "", info_builder.build())
        else:
            robot.client.send_private_msg(robot.user_id, info_builder.build())

        # Send the music share card — this renders as a beautiful playable card in QQ
        music_builder = MessageBuilder()
        music_builder.music(music_type, str(song_id))
        if robot.msg_type == "group":
            robot.client.send_group_msg(robot.group_id or "", music_builder.build())
        else:
            robot.client.send_private_msg(robot.user_id, music_builder.build())

        logger.info("Music shared: %s - %s (id=%s, type=%s)", name, artist, song_id, music_type)

        # ── 3. Try audio download as bonus (best-effort) ──
        try:
            audio_path = self.service.download_audio(
                song_info.get("audio_url", ""), song_id
            )
            if audio_path:
                record_builder = MessageBuilder()
                record_builder.record(audio_path)
                if robot.msg_type == "group":
                    robot.client.send_group_msg(robot.group_id or "", record_builder.build())
                else:
                    robot.client.send_private_msg(robot.user_id, record_builder.build())
                logger.info("Audio voice message also sent for %s - %s", name, artist)
        except Exception:
            logger.debug("ai_tools.music_search_call 忽略了异常", exc_info=True)

        _set_tool_meta(ai, tool_calls)
        ai.user_text = f"播放歌曲: {name} - {artist}"


# ══════════════════════════════════════════════════════════
#  Sticker Battle (斗图)
# ══════════════════════════════════════════════════════════

BATTLE_DEFAULT_ROUNDS = 5


class StickerBattleTool:
    """Tool handler for initiating sticker battles (斗图).

    Triggered when AI recognizes user intent to start a sticker battle.
    Picks a random sticker, sends it as the first salvo with a challenge
    message, and sets up battle state for subsequent rounds.

    Battle state (dict stored in memory, shared with main.py):
        - round: current round number (starts at 1)
        - max_rounds: total rounds for this battle
        - started_at: timestamp of last activity
        - user_id / group_id: identifying info
        - active: whether battle is still ongoing
        - used_stickers: list of sticker filenames already sent by bot
        - total_score: accumulated score across rounds
    """

    def __init__(self, client: Any, sticker_tool: StickerTool, battle_state: dict) -> None:
        self.client = client
        self.sticker_tool = sticker_tool
        self.battle_state = battle_state

    def _pick_random_sticker(self) -> str | None:
        """Pick a random sticker filename from the collection."""
        import os as _os
        stickerdir = self.sticker_tool.STICKER_DIR
        try:
            files = [
                f for f in _os.listdir(stickerdir)
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
            ]
            if files:
                return random.choice(files)
        except Exception:
            logger.exception("Failed to scan stickers for battle")
        return None

    def sticker_battle_call(self, robot: Any, ai: Any) -> None:
        import time as _time

        battle_key = f"{robot.user_id}:{robot.group_id or 'private'}"

        # Check if already in battle
        if battle_key in self.battle_state and self.battle_state[battle_key].get("active"):
            robot.reply("已经在斗图中啦！发你的表情包过来吧～(๑•̀ㅂ•́)و✧")
            _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
            ai.user_text = "斗图已在进行中"
            return

        # Pick a random sticker for the bot's first salvo
        chosen = self._pick_random_sticker()
        if not chosen:
            robot.reply("呜呜～我的表情包库存不足，斗图失败！(｡•́︿•̀｡)")
            _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
            ai.user_text = "斗图启动失败：库存不足"
            return

        # Set up battle state
        battle = {
            "round": 1,
            "max_rounds": BATTLE_DEFAULT_ROUNDS,
            "started_at": _time.time(),
            "user_id": robot.user_id,
            "group_id": robot.group_id,
            "active": True,
            "used_stickers": [chosen],
            "total_score": 0,
        }
        self.battle_state[battle_key] = battle

        # Send first sticker + challenge message
        from qq_official import MessageBuilder
        builder = MessageBuilder()
        builder.image(f"{self.sticker_tool.STICKER_DIR}/{chosen}")
        challenge = random.choice([
            "来斗图吧！谁怕谁！٩(◕‿◕)۶",
            "接招！这是我珍藏的表情包！",
            "斗图开始！放马过来～(๑˃̵ᴗ˂̵)و",
            "哼！让你见识见识我的厉害！",
        ])
        builder.text(f"\n{challenge} (第1/{BATTLE_DEFAULT_ROUNDS}轮)")
        if robot.msg_type == "group":
            self.client.send_group_msg(robot.group_id or "", builder.build())
        else:
            self.client.send_private_msg(robot.user_id, builder.build())

        logger.info("Battle started for %s (key=%s), first sticker: %s", robot.user_name, battle_key, chosen)

        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))
        ai.user_text = f"启动了斗图模式，发送了第一张表情包: {chosen}"


# ══════════════════════════════════════════════════════════
#  Affection — Check affection score
# ══════════════════════════════════════════════════════════

class AffectionTool:
    """FOLLOW_UP tool: returns affection data for AI to format as reply."""

    def __init__(self, database_manager: Any) -> None:
        self.db = database_manager

    def check_affection_call(self, robot: Any, ai: Any) -> None:
        from affection_service import AffectionService

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        # Parse target_name
        target_name = ""
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
                target_name = (args.get("target_name") or "").strip()
            except (json.JSONDecodeError, TypeError):
                logger.debug("ai_tools.check_affection_call 忽略了异常", exc_info=True)

        # Determine target user
        if not target_name or target_name in ("我", "自己", "我的"):
            target_uid = robot.user_id
            target_display = robot.user_name
        else:
            target_uid, target_display = self.db.find_member_by_name(
                robot.group_id or "", target_name,
            )
            if not target_uid:
                ai.tool_result_text = f"找不到群友「{target_name}」哦～可能ta还没说过话？"
                ai.user_text = ai.tool_result_text
                return

        # Fetch affection data
        record = AffectionService.get_or_create(
            self.db, target_uid or "", robot.group_id or "", target_display or target_name,
        )
        score = record["affection_score"]
        label, emoji = AffectionService.get_relationship(score)

        is_self = (target_uid == robot.user_id) or (not target_name)

        if is_self:
            ai.tool_result_text = (
                f"查询用户 {target_display} 的好感度结果：\n"
                f"好感度：{score:.0f}/100 {emoji}{label}\n"
                f"互动次数：{record['interaction_count']}次\n"
                f"好评：{record['positive_count']}次 | 差评：{record['negative_count']}次\n"
                f"关系备注：{record.get('notes') or '无'}"
            )
        else:
            ai.tool_result_text = (
                f"查询群友 {target_display} 对 Kiriko 的好感度结果：\n"
                f"好感度：{score:.0f}/100 {emoji}{label}\n"
                f"互动次数：{record['interaction_count']}次"
            )
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Affection Leaderboard
# ══════════════════════════════════════════════════════════

class AffectionLeaderboardTool:
    """FOLLOW_UP tool: returns leaderboard for AI to format as reply."""

    def __init__(self, database_manager: Any) -> None:
        self.db = database_manager

    def affection_leaderboard_call(self, robot: Any, ai: Any) -> None:
        from affection_service import AffectionService

        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        board = AffectionService.get_leaderboard(
            self.db, group_id=robot.group_id, limit=10,
        )

        if not board:
            ai.tool_result_text = "这个群还没有好感度数据哦～多和我聊天互动吧！(◕‿◕✿)"
            ai.user_text = ai.tool_result_text
            return

        lines = [f"📊 {robot.group_name or '本群'} 好感度排行榜 TOP{len(board)}"]
        for i, entry in enumerate(board, 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            lines.append(
                f"{medal} {entry['user_name']} — "
                f"{entry['affection_score']:.0f}分 {entry['emoji']}{entry['relationship']} "
                f"({entry['interaction_count']}次互动)"
            )

        ai.tool_result_text = "\n".join(lines)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Recall (撤回机器人自己的消息)
# ══════════════════════════════════════════════════════════

class RecallMessageTool:
    """FOLLOW_UP tool: recall the bot's own most recent message in this group.

    QQ only lets a member recall their own message for about two minutes, so
    the lookup is bounded by that window instead of failing at the API.
    """

    RECALL_WINDOW = 110  # seconds, comfortably inside QQ's ~2 minute limit

    def __init__(self, database_manager: Any, client: Any) -> None:
        self.db = database_manager
        self.client = client

    def recall_message_call(self, robot: Any, ai: Any) -> None:
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))

        if robot.msg_type != "group" or not robot.group_id:
            ai.tool_result_text = "只能在群聊里撤回消息。"
            ai.user_text = ai.tool_result_text
            return

        last = self.db.get_last_bot_message(robot.group_id, self.RECALL_WINDOW)
        if not last:
            ai.tool_result_text = (
                "你最近两分钟内没有在这个群发过消息，或者那条已经撤回了，没有可撤回的内容。"
                "直接告诉对方没有可撤回的消息就行，不要假装撤回了。"
            )
            ai.user_text = ai.tool_result_text
            return

        if self.client.recall(last["message_id"]):
            self.db.mark_bot_message_recalled(last["message_id"])
            preview = (last["text"] or "").strip()[:20]
            logger.info("Recalled own message %s in group %s", last["message_id"], robot.group_id)
            ai.tool_result_text = (
                f"已成功撤回你刚才发的那条消息（开头是「{preview}」）。"
                "用很自然的语气应一声就好，比如「好啦撤回啦」。"
            )
        else:
            logger.warning("Recall failed for message %s", last["message_id"])
            ai.tool_result_text = (
                "撤回失败了（可能超过了 QQ 的两分钟限制）。"
                "用轻松的语气说明一下就行，不要反复重试。"
            )
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Feature list (群友提交的功能需求)
# ══════════════════════════════════════════════════════════

class FeatureListTool:
    """FOLLOW_UP tool: what has been requested, and where it stands."""

    STATUS_LABEL = {"pending": "待处理", "done": "已完成", "rejected": "已拒绝"}

    def __init__(self, database_manager: Any) -> None:
        self.db = database_manager

    def feature_list_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        _set_tool_meta(ai, tool_calls)

        args: dict[str, Any] = {}
        if tool_calls:
            try:
                args = json.loads(tool_calls[0]["function"].get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                args = {}

        status = str(args.get("status") or "pending").strip().lower()
        if status in ("all", "全部", ""):
            status = ""

        try:
            items = self.db.get_feature_requests(status=status, limit=15)
        except Exception:
            logger.exception("feature_list failed")
            ai.tool_result_text = "查询功能清单失败了。"
            ai.user_text = ai.tool_result_text
            return

        if not items:
            label = self.STATUS_LABEL.get(status, "该状态")
            ai.tool_result_text = (
                f"目前没有「{label}」的功能需求。"
                "用自然的语气告诉对方就好，不要编造需求。"
            )
            ai.user_text = ai.tool_result_text
            return

        lines = ["功能需求清单（真实数据，可据此回答）："]
        for it in items:
            tag = self.STATUS_LABEL.get(it["status"], it["status"])
            lines.append(f"- [{tag}] {it['summary']}（{it['timestamp']} 提交）")
        lines.append("用 Kiriko 的语气概括给用户听，不要逐条照念。")

        ai.tool_result_text = "\n".join(lines)
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Explain self (调用链 + 思维链)
# ══════════════════════════════════════════════════════════

class ExplainSelfTool:
    """DEBUG tool: dump the PREVIOUS reply's raw record straight into the chat.

    The current turn is not saved until after the reply is sent, so the newest
    assistant row in `history` is exactly the message the user is asking about.

    This is a *debugging* aid, so it deliberately bypasses the model: feeding
    the chain back through the AI made it re-tell its own thoughts in its own
    words, which is a lossy second pass over the very text we are trying to
    inspect. Instead the reasoning is reproduced verbatim — newlines kept, no
    summarising, no rewording — and sent directly, with no follow-up turn.
    """

    MAX_TOTAL = 8000      # matches the history.reasoning storage cap
    CHUNK = 1200          # keep each QQ text segment comfortably small

    def __init__(self, database_manager: Any) -> None:
        self.db = database_manager

    def explain_self_call(self, robot: Any, ai: Any) -> None:
        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))

        group_id = robot.group_id if robot.msg_type == "group" else None
        try:
            last = self.db.get_last_bot_turn(robot.user_id, group_id)
        except Exception:
            logger.exception("explain_self lookup failed")
            last = None

        if not last:
            self._send(robot, "【执行回放】还没有上一轮的记录，无从查阅。")
            ai.tool_result_text = "已直接告知用户没有上一轮记录。本轮不要再回复任何内容。"
            ai.user_text = ai.tool_result_text
            return

        self._send(robot, self._render(last))
        # Self-contained: main_logic sends no follow-up, so the model never
        # gets a chance to paraphrase what we just dumped.
        ai.tool_result_text = (
            "已把上一轮的原始记录直接发到对话里（原文照录）。"
            "本轮不要再说任何话，也不要复述其中的内容。"
        )
        ai.user_text = ai.tool_result_text

    def _render(self, last: dict[str, Any]) -> str:
        lines = ["【上一轮原始记录 · 调试输出】"]
        if last.get("timestamp"):
            lines.append(f"时间：{last['timestamp']}")

        reasoning = (last.get("reasoning") or "").strip()
        lines.append("")
        lines.append("── 思维链原文 ──")
        lines.append(reasoning or "（这一轮没有思维链：思考模式可能被关掉了）")

        chain: list[dict[str, Any]] = []
        if last.get("tool_calls"):
            try:
                chain = json.loads(last["tool_calls"])
            except (json.JSONDecodeError, TypeError):
                chain = []
        lines.append("")
        lines.append("── 工具调用 ──")
        if chain:
            for i, c in enumerate(chain, 1):
                name = c.get("name", "?")
                args = (c.get("arguments") or "").strip()
                lines.append(f"{i}. {name}({args})")
        else:
            lines.append("（无，直接回答的）")

        reply = (last.get("content") or "").strip()
        lines.append("")
        lines.append("── 最终回复 ──")
        lines.append(reply or "（空）")

        text = "\n".join(lines)
        if len(text) > self.MAX_TOTAL:
            text = text[:self.MAX_TOTAL] + "\n…（超出 8000 字，已截断）"
        return text

    def _split(self, text: str) -> list[str]:
        """Chunk on line boundaries so raw reasoning stays readable.

        A thinking chain is often one enormous unbroken paragraph, so lines
        longer than CHUNK are hard-wrapped too — otherwise the whole point of
        chunking (QQ rejects oversized text segments) is lost.
        """
        chunks: list[str] = []
        current = ""
        for line in text.split("\n"):
            while len(line) > self.CHUNK:
                if current:
                    chunks.append(current)
                    current = ""
                chunks.append(line[:self.CHUNK])
                line = line[self.CHUNK:]
            candidate = line if not current else current + "\n" + line
            if len(candidate) > self.CHUNK:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [text]

    def _send(self, robot: Any, text: str) -> None:
        from qq_official import MessageBuilder

        chunks = self._split(text)
        total = len(chunks)
        for idx, chunk in enumerate(chunks, 1):
            body = f"({idx}/{total})\n{chunk}" if total > 1 else chunk
            builder = MessageBuilder()
            # Quote the request once so the dump is anchored in a busy group.
            if idx == 1 and robot.incoming.message_id:
                builder.reply(robot.incoming.message_id)
            builder.text(body)
            try:
                if robot.msg_type == "group":
                    robot.client.send_group_msg(robot.group_id or "", builder.build())
                else:
                    robot.client.send_private_msg(robot.user_id, builder.build())
            except Exception:
                logger.exception("explain_self send failed")
                return


# ══════════════════════════════════════════════════════════
#  Similar sticker (感知哈希找最像的一张)
# ══════════════════════════════════════════════════════════

class SimilarStickerTool:
    """FOLLOW_UP tool: send the library sticker closest to the user's image.

    Reuses the perceptual-hash index the collector already maintains for
    de-duplication, so this costs nothing extra to build.
    """

    def __init__(self, collector: Any) -> None:
        self.collector = collector

    def similar_sticker_call(self, robot: Any, ai: Any) -> None:
        import os

        import imagehash
        import requests as _requests

        from sticker_collector import PHASH_THRESHOLD, STICKER_DIR, StickerCollector

        _set_tool_meta(ai, ai.ai_message.get("tool_calls"))

        urls = robot.incoming.image_urls
        if not urls:
            ai.tool_result_text = (
                "这条消息里没有图片，没法找相似表情。"
                "如实告诉对方需要先发一张图即可。"
            )
            ai.user_text = ai.tool_result_text
            return

        target = None
        try:
            resp = _requests.get(urls[0], timeout=10)
            resp.raise_for_status()
            target = StickerCollector._phash_data(resp.content)
        except Exception:
            logger.debug("similar sticker: hash failed", exc_info=True)

        if not target:
            ai.tool_result_text = "没能识别这张图（可能不是常见格式），换一张试试。"
            ai.user_text = ai.tool_result_text
            return

        try:
            target_hash = imagehash.hex_to_hash(target)
        except Exception:
            ai.tool_result_text = "这张图算不出相似度，换一张试试。"
            ai.user_text = ai.tool_result_text
            return

        best_file, best_dist = None, 999
        for phash_hex, filename in (self.collector.phashes or {}).items():
            try:
                dist = target_hash - imagehash.hex_to_hash(phash_hex)
            except Exception:
                continue
            if dist < best_dist:
                best_file, best_dist = filename, dist

        if not best_file or best_dist > PHASH_THRESHOLD:
            ai.tool_result_text = (
                "表情库里没有找到足够相似的表情（这是真实结果）。"
                "用自然的语气说没找到就行，不要编造。"
            )
            ai.user_text = ai.tool_result_text
            return

        path = os.path.join(STICKER_DIR, best_file)
        if not os.path.exists(path):
            ai.tool_result_text = "找到了相似表情但文件不见了，如实说明即可。"
            ai.user_text = ai.tool_result_text
            return

        try:
            from qq_official import MessageBuilder
            builder = MessageBuilder().image(path)
            if robot.msg_type == "group":
                robot.client.send_group_msg(robot.group_id or "", builder.build())
            else:
                robot.client.send_private_msg(robot.user_id, builder.build())
            logger.info("Similar sticker sent: %s (distance %d)", best_file, best_dist)
            ai.tool_result_text = (
                f"已经发出表情库里最像的一张（差异值 {best_dist}，越小越像）。"
                "用一句话配一下就行，不要再重复发图。"
            )
        except Exception:
            logger.exception("similar sticker send failed")
            ai.tool_result_text = "发送相似表情失败了，如实说明即可。"
        ai.user_text = ai.tool_result_text


# ══════════════════════════════════════════════════════════
#  Amp head library (on demand)
# ══════════════════════════════════════════════════════════

class AmpHeadTool:
    """吉他箱头资料库——**按需**推荐一条。

    以前这是每天定时往群里推一条（`scheduler._build_amp_head` + 推送订阅的
    `amp_head` 话题）。官方平台没有主动推送，那条路径整个删掉了，于是这个
    资料库在界面上只剩「箱头库」页能翻。改成工具后，群友直接问就能拿到，
    资料库也重新有了用处。

    仍然复用 `db.get_amp_head_of_the_day()`：按 day-of-year 在库里轮转，
    保证一轮之内不重复（比 ORDER BY RANDOM() 好——随机会连着两天抽到同一个，
    看起来像 bug），而且同一天所有人拿到的是同一个箱头，方便群里聊起来。
    """

    def __init__(self, db: Any) -> None:
        self.db = db

    def amp_head_call(self, robot: Any, ai: Any) -> None:
        tool_calls = ai.ai_message.get("tool_calls")
        try:
            head = self.db.get_amp_head_of_the_day()
        except Exception:
            logger.exception("amp_head lookup failed")
            head = None

        if not head:
            ai.tool_result_text = "箱头资料库里暂时没有可以推荐的条目，如实说一声就行。"
            _set_tool_meta(ai, tool_calls)
            ai.user_text = ai.tool_result_text
            return

        brand = (head.get("brand") or "").strip()
        model = (head.get("model") or "").strip()
        name = " ".join(p for p in (brand, model) if p) or "某台箱头"

        lines = [f"今天的箱头：{name}"]
        # 只列真正有值的字段——资料库是从 Wikipedia 整理的，个别条目会缺项，
        # 空字段硬写会变成「年份：」这种半截话。
        labels = [
            ("year", "年份"), ("origin", "产地"), ("kind", "类型"),
            ("power", "功率"), ("tubes", "电子管"), ("tone", "音色特点"),
            ("famous", "用过它的名人"), ("tip", "小贴士"),
        ]
        for field, label in labels:
            val = str(head.get(field) or "").strip()
            if val:
                lines.append(f"- {label}：{val}")
        price = str(head.get("price") or "").strip()
        if price:
            # 价格是从公开资料整理的，会随时间变化，必须标明是参考值
            lines.append(f"- 参考价（约，可能已过时）：{price}")
        if (head.get("source") or "") == "wikipedia":
            lines.append("（资料由 Wikipedia 词条整理）")

        ai.tool_result_text = "\n".join(lines)
        _set_tool_meta(ai, tool_calls)
        ai.user_text = ai.tool_result_text
