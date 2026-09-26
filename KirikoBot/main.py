from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from ai_server import AiServer
from ai_tools import (
    Tarot, Tarot_History, GamingNews,
    WebSearchTool, WeatherTool, StickerTool,
    HitokotoTool, FoodPickerTool, DiceTool, BilibiliTool,
    TimeTool, PoliticalNewsTool,
    BalanceTool, FeatureRequestTool, MusicTool,
    StickerBattleTool, BATTLE_DEFAULT_ROUNDS,
    AffectionTool, AffectionLeaderboardTool,
    RecallMessageTool,
    FeatureListTool, ExplainSelfTool, SimilarStickerTool, AmpHeadTool,
)
from affection_service import AffectionService
from balance_service import BalanceService
from ai_tools_list import AiTools
from config import Config
import ai_metrics
import dashboard_auth
from chat_history import load_history, save_turn
from prompt_builder import (
    build_role_prompt,
    deflection_for,
    filler_for,
    leaked_persona,
    quote_ref_id,
    resolve_quote,
    build_system_prompt as _build_system_prompt,
    build_user_message as _context,
)
from database_manager import DatabaseManager
from feature_gate import (
    FEATURE_DEFS, FeatureGate, VALID_SCOPES,
    disabled_tool_names, disabled_labels,
)
from extra_services import HitokotoService, BilibiliTrending
from hot_news import HotNewsScraper
from judge_service import JudgeService
from qq_gateway import GatewayClient
from qq_official import MessageBuilder, QQOfficialClient
from news_crawler import NewsCrawler
from log_stream import sse_handler, setup_sse_logging
from learning_service import LearningService
from music_service import MusicService
from political_news import PoliticalNewsScraper
from profile_service import ProfileService
from robot_server import RobotServer
from scheduler import BotScheduler
from sticker_collector import StickerCollector, STICKER_DIR, STICKER_CATEGORIES
from version_manager import VersionManager
from web_search import WebSearch
from weather_service import WeatherService


def _setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    if root.handlers:
        for x in root.handlers: root.removeHandler(x)
    root.addHandler(h)

_setup_logging()
setup_sse_logging()
logger = logging.getLogger(__name__)

# ── App ─────────────────────────────────────────────────
app = Flask(__name__)
# The dashboard shell is edited often; re-read templates from disk instead of
# caching them for the process lifetime (Flask's production default).
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
dashboard_auth.init_app(app)

# ── Sticker battle state (must be before services that reference it) ──
_battle_state: dict[str, dict] = {}  # "user_id:group_id" → battle info
_battle_lock = threading.Lock()
BATTLE_TIMEOUT = 60  # seconds before battle auto-ends

# ── Services ────────────────────────────────────────────
client = QQOfficialClient(Config.QQ_APP_ID or "", Config.QQ_APP_SECRET or "")
db = DatabaseManager()
feature_gate = FeatureGate(db)
tools_def = AiTools()

tarot = Tarot(db)
tarot_history = Tarot_History(db)
news_crawler = NewsCrawler()
gaming_news = GamingNews(news_crawler)
web_search = WebSearch()
web_search_tool = WebSearchTool(web_search)
weather_tool = WeatherTool(WeatherService())
sticker_tool = StickerTool()
sticker_battle_tool = StickerBattleTool(client, sticker_tool, _battle_state)
hitokoto_service = HitokotoService()
hitokoto_tool = HitokotoTool(hitokoto_service)
food_picker_tool = FoodPickerTool()
dice_tool = DiceTool()
bilibili_tool = BilibiliTool(BilibiliTrending())
time_tool = TimeTool()
political_news_scraper = PoliticalNewsScraper()
political_news_tool = PoliticalNewsTool(political_news_scraper)
balance_service = BalanceService()
balance_tool = BalanceTool(balance_service)
feature_request_tool = FeatureRequestTool(db)
music_service = MusicService()
music_tool = MusicTool(music_service)
hot_news_scraper = HotNewsScraper()

scheduler = BotScheduler(db, client, feature_gate=feature_gate)
scheduler.start()
sticker_collector = StickerCollector(db=db)
profile_service = ProfileService()
learning_service = LearningService()
affection_service = AffectionService()
judge_service = JudgeService(learning_service)
affection_tool = AffectionTool(db)
affection_leaderboard_tool = AffectionLeaderboardTool(db)
recall_tool = RecallMessageTool(db, client)
feature_list_tool = FeatureListTool(db)
explain_self_tool = ExplainSelfTool(db)
similar_sticker_tool = SimilarStickerTool(sticker_collector)
amp_head_tool = AmpHeadTool(db)

# Persist the bot's own outgoing messages so transcripts are complete and
# "recall the last thing I said" works across restarts.
client.set_recorder(db.record_bot_message)

# Every DeepSeek call (chat / background jobs / vision) is recorded for the
# dashboard's usage page. Never on the critical path — metrics failures are
# swallowed inside ai_metrics.
if Config.AI_METRICS_ENABLED:
    ai_metrics.set_sink(db.record_ai_call)
version_manager = VersionManager(db)
version_manager.seed_initial_version()

# 安全姿态明说一次：官方平台的防线是「出站 WebSocket + access_token」，
# 不再有需要校验签名的入站 webhook。面板本身仍有 Basic 鉴权。
logger.info(
    "接入方式：QQ 官方平台 WebSocket（出站长连接）。"
    "没有入站 webhook 路由，机器人只应答官方推送过来的 @ 消息。"
)

# Dedicated logger for thinking chains — propagates to root (SSE + stdout)
think_log = logging.getLogger("think")

executor = ThreadPoolExecutor(max_workers=12)
_seeded_groups: set[str] = set()
_start_time = time.time()

# ── Sticker understanding state ─────────────────────────
_sticker_pending: dict[str, float] = {}  # "user_id:group_id" → timestamp
_sticker_pending_lock = threading.Lock()
STICKER_REQUEST_TIMEOUT = 30  # seconds

def _request_sticker_call(robot: Any, ai: Any) -> None:
    """request_sticker tool: arm the 2-step image flow.
    Only registers pending state — the natural-language invite is written by the
    AI follow-up turn (this tool is NOT in SELF_CONTAINED_TOOLS)."""
    pending_key = f"{robot.user_id}:{robot.group_id or 'private'}"
    with _sticker_pending_lock:
        _sticker_pending[pending_key] = time.time()
    ai.tool_result_text = (
        "已进入等待图片状态（30秒内有效）。请用 Kiriko 的语气友好地请用户把图片/表情包发过来，"
        "一句话即可（可带颜文字），不要编造图片内容。"
    )
    logger.info("🎯 Sticker flow: request_sticker armed for %s", robot.user_name)

# ── Tool routing ────────────────────────────────────────
ROUTES = {
    "tarot": tarot.tarot_call, "tarot_history": tarot_history.tarot_history_call,
    "gaming_news": gaming_news.gaming_news_call, "web_search": web_search_tool.web_search_call,
    "weather": weather_tool.weather_call, "sticker": sticker_tool.sticker_call,
    "request_sticker": _request_sticker_call,
    "hitokoto": hitokoto_tool.hitokoto_call, "food_picker": food_picker_tool.food_picker_call,
    "dice": dice_tool.dice_call, "bilibili_trending": bilibili_tool.bilibili_call,
    "get_current_time": time_tool.get_current_time_call,
    "political_news": political_news_tool.political_news_call,
    "check_balance": balance_tool.balance_call,
    "submit_feature": feature_request_tool.feature_request_call,
    "music_search": music_tool.music_search_call,
    "sticker_battle": sticker_battle_tool.sticker_battle_call,
    "check_affection": affection_tool.check_affection_call,
    "affection_leaderboard": affection_leaderboard_tool.affection_leaderboard_call,
    "recall_message": recall_tool.recall_message_call,
    "feature_list": feature_list_tool.feature_list_call,
    "explain_self": explain_self_tool.explain_self_call,
    "similar_sticker": similar_sticker_tool.similar_sticker_call,
    "amp_head": amp_head_tool.amp_head_call,
}

# Self-contained tools format and send their own reply — no AI follow-up needed
SELF_CONTAINED_TOOLS = {
    "tarot", "sticker", "web_search",
    "political_news", "gaming_news", "bilibili_trending",
    "hitokoto", "tarot_history", "music_search", "sticker_battle",
    # explain_self sends the raw debug dump itself; a follow-up turn would only
    # add the model's paraphrase on top of the text we want verbatim.
    "explain_self",
}

# ── History (only recent context, filtered for clarity) ──
# History storage/replay lives in chat_history.py so it can be unit-tested
# (importing main would start the scheduler). These stay as thin wrappers so
# call sites keep working against the process-wide db.
def _load_history(uid: str, gid: str | None) -> list[dict[str, Any]]:
    return load_history(db, uid, gid)

def _save_turn(uid: str, gid: str | None, user_msg: str, ai_text: str,
               reasoning: str = "", tool_chain: str = "",
               handled: bool = False) -> None:
    save_turn(db, uid, gid, user_msg, ai_text, reasoning, tool_chain, handled)

# ── Group seeding ───────────────────────────────────────
def _seed_group(gid: str) -> None:
    if gid in _seeded_groups:
        return
    _seeded_groups.add(gid)
    try:
        members = client.get_group_member_list(gid)
        if members:
            db.seed_group_members(gid, members)
    except Exception:
        logger.debug("main._seed_group 忽略了异常", exc_info=True)

# ── Core logic ──────────────────────────────────────────

def _enabled_tools(disabled: set[str] | None = None) -> list[dict[str, Any]]:
    """Every AI tool minus those whose feature is disabled for this scope.
    All tool selection now happens in the model via native function calling."""
    banned = disabled_tool_names(disabled or set())
    return [t for t in tools_def.ai_tools() if t["function"]["name"] not in banned]


def _tool_chain_json(tool_calls: Any) -> str:
    """Compact, dashboard-friendly record of this turn's tool calls."""
    if not tool_calls:
        return ""
    items = tool_calls if isinstance(tool_calls, list) else [tool_calls]
    chain = []
    for tc in items:
        fn = tc.get("function", {}) if isinstance(tc, dict) else {}
        chain.append({"name": fn.get("name", ""), "arguments": fn.get("arguments", "")})
    try:
        return json.dumps(chain, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return ""


def _reply_note(robot: RobotServer) -> str:
    """Describe the quoted message when the incoming one is a reply.

    官方平台的引用信息带正文和昵称，但昵称认不出「引用的是我自己说给谁的话」。
    所以被引用的消息还会用我们自己的记录（bot_messages / group_messages）反查一遍
    —— 那次反查才是「另一个用户引用了机器人刚才说给别人的话」能被识别的关键。
    """
    reply = getattr(robot.incoming, "reply", None)
    if reply is None:
        return ""

    ref_id = quote_ref_id(reply)
    try:
        is_own = client.is_own_message(ref_id, reply.text)
    except Exception:
        logger.debug("is_own_message failed", exc_info=True)
        is_own = False

    note = resolve_quote(reply, is_own,
                         lambda mid: db.find_quoted(robot.group_id, mid,
                                                    quoted_text=reply.text),
                         current_user=robot.user_name or "")
    # Quote awareness is otherwise invisible: if the lookup misses, the bot just
    # answers as though nothing were quoted, and there is no error to notice.
    if note:
        logger.info("引用感知命中（id=%s）：%s", ref_id, note[:100])
    elif ref_id is not None:
        logger.info("引用感知未命中：id=%s 不在库里（无法还原被引用的内容）", ref_id)
    return note

def _mood_signal(robot: RobotServer) -> str:
    """Tell the model how hard this user has been leaning on it.

    The persona's temper is meant to escalate across a *conversation*, but
    every request is independent: the model sees only the last few turns, so
    it cannot tell "first question today" from "the sixth time in five
    minutes". The count is computed here and handed over as a fact, which is
    what makes the escalation actually advance instead of restarting at polite
    every turn.
    """
    if not robot.user_id:
        return ""
    try:
        info = db.get_mood(
            robot.user_id, robot.group_id, text=robot.msg,
            pressure_minutes=Config.PATIENCE_WINDOW_MINUTES,
            cooldown_minutes=Config.MOOD_COOLDOWN_MINUTES,
        )
    except Exception:
        logger.debug("mood signal failed", exc_info=True)
        return ""
    if info["level"] <= 0:
        return ""
    detail = f"最近 {Config.PATIENCE_WINDOW_MINUTES} 分钟这个用户找了你 {info['count']} 次"
    if info["repeats"]:
        detail += f"，其中 {info['repeats']} 次是同一件事"
    # Deliberately advisory. Spelling out "you are now at level 3, act like it"
    # made the bot recite the count back instead of just having a mood, which
    # read as mechanical.
    line = (f"【你现在的状态】{detail}（仅供参考：可能会有点「{info['label']}」）。"
            "这只是让你知道自己被磨了多久，**别刻意照着演，也别把这个次数说出来**。")
    if info.get("cooling"):
        line += f"而且你气已经消得差不多了（大约 {Config.MOOD_COOLDOWN_MINUTES} 分钟回到正常），别翻旧账。"
    return line


def _log_thinking(user_name: str, reasoning: str) -> None:
    """Log thinking chain to dedicated logger (visible in logs + frontend)."""
    if not reasoning:
        return
    # Truncate very long chains for readability
    preview = reasoning[:800] + "…" if len(reasoning) > 800 else reasoning
    think_log.info("【%s】%s", user_name, preview)



def _process_sticker_analysis(robot: RobotServer, image_url: str) -> None:
    """Analyze a sticker/image via vision API end-to-end.

    Flow: Vision API directly generates Kiriko-style reply (single call).
    No DeepSeek involvement — the vision model handles understanding + reply.
    Falls back to context-based DeepSeek response only if vision is unavailable.
    Sticker categorization runs asynchronously in background.
    """
    try:
        reply_text: str | None = None

        # Step 1: Vision API end-to-end (understand image + generate Kiriko reply)
        if Config.VISION_ENABLED:
            try:
                role = (
                    Config.GROUP_ROLE
                    if robot.msg_type == "group"
                    else Config.PRIVATE_ROLE
                )
                reply_text = AiServer.vision_chat_reply(
                    image_url_or_path=image_url,
                    role_prompt=build_role_prompt(role),
                    user_name=robot.user_name,
                    user_text=robot.msg.strip(),
                )
                if reply_text:
                    logger.info(
                        "Vision end-to-end: %s → %s",
                        robot.user_name, reply_text[:40],
                    )
                else:
                    logger.warning("Vision API returned None for reply")
            except Exception:
                logger.exception("Vision end-to-end reply failed, falling back")

        # Step 2: Fallback — context-based DeepSeek response
        if not reply_text:
            user_text = f"用户 {robot.user_name} 发了一个表情包/图片。"
            if robot.msg.strip():
                user_text += f" 用户同时说：{robot.msg.strip()}"
            system_text = (
                build_role_prompt(Config.GROUP_ROLE) + "\n"
                "有群友发了一张表情包/图片。你看不到图片内容，"
                "请根据上下文对这张表情包做出回应，30字以内。"
            )
            ai = AiServer(
                system_text=system_text,
                user_text=user_text,
                history_list=[],
                tools=[],
                model_type=Config.DEEPSEEK_MODEL,
                thinking_type="disabled",
            )
            ai.ai_request()
            reply_text = ai.ai_text.strip() if ai.ai_text else "收到表情包啦～好可爱！(◕‿◕✿)"

        robot.reply(reply_text)

        # Step 3: Background sticker categorization (best-effort, non-blocking)
        if Config.VISION_ENABLED:
            executor.submit(_background_sticker_categorize, image_url)

        # Record to chat history
        _save_turn(robot.user_id, robot.group_id, "[图片消息]", reply_text)

    except Exception:
        logger.exception("Sticker analysis failed for %s", image_url[:60])
        try:
            robot.reply("收到表情包啦～(◕‿◕✿)")
        except Exception:
            logger.debug("main._process_sticker_analysis 忽略了异常", exc_info=True)


def _background_sticker_categorize(image_url: str) -> None:
    """Best-effort background sticker categorization via vision API.

    Runs after the end-to-end reply is already sent, so this does not
    block the user-facing response time.

    Only touches stickers that are already in the library: the incoming image
    is matched against `stickers` by filename, and an already-categorized one
    is skipped so we don't pay for a second vision call just to re-derive what
    we know. Images that aren't in the library are ignored — the passive
    collection path that used to add them is gone (官方平台收不到非 @ 群消息),
    so the library is static.
    """
    try:
        match = None
        for s in db.get_stickers():
            fn = s.get("filename", "")
            if fn and (fn in image_url or image_url.endswith(fn)):
                match = s
                break
        if match and match.get("category") not in ("", "未分类"):
            return
        vision_data = AiServer.vision_analyze_with_category(image_url)
        if not vision_data or not match:
            return
        db.update_sticker_category(
            match["filename"],
            vision_data.get("category", "未分类"),
            vision_data.get("description", ""),
            vision_data.get("emotion", ""),
        )
    except Exception:
        logger.debug("main._background_sticker_categorize 忽略了异常", exc_info=True)


# ── Sticker battle handlers ──────────────────────────

def _process_battle_round(robot: RobotServer, battle_key: str, battle: dict, image_url: str) -> None:
    """Process one round of sticker battle.

    Uses vision API to score the user's sticker, then either ends the battle
    (last round — declare winner) or sends a counter-sticker with witty comeback.
    """
    try:
        round_num = battle["round"]
        max_rounds = battle["max_rounds"]
        is_last = round_num >= max_rounds

        # Call vision API to rate + generate comeback
        role = Config.GROUP_ROLE if robot.msg_type == "group" else Config.PRIVATE_ROLE
        battle_result = AiServer.vision_sticker_battle(
            image_url_or_path=image_url,
            role_prompt=role or "",
            user_name=robot.user_name,
            round_num=round_num,
        )

        score = battle_result["score"] if battle_result else 5
        comment = (battle_result.get("comment", "") or "") if battle_result else ""
        comeback = (battle_result.get("comeback", "") or "哼！看我的！") if battle_result else "哼！看我的！"

        # Send evaluation of user's sticker
        eval_parts = [f"第{round_num}轮：你的表情包得分 {score}/10！"]
        if comment:
            eval_parts.append(comment)
        robot.reply("\n".join(eval_parts))

        # Accumulate score
        battle["total_score"] = battle.get("total_score", 0) + score

        if is_last:
            # ── Battle over — declare winner ──
            total_score = battle["total_score"]
            max_possible = max_rounds * 10
            if total_score > max_rounds * 5:
                winner_line = "你赢了！Kiriko甘拜下风～下次再来！(◕‿◕✿)"
            elif total_score < max_rounds * 5:
                winner_line = "哈哈哈还是我赢了！下次再来战！(๑•̀ㅂ•́)و✧"
            else:
                winner_line = "平局！棋逢对手啊～打得难分难解！"

            summary = (
                f"斗图结束！你的总得分：{total_score}/{max_possible}\n"
                f"{winner_line}"
            )
            robot.send_text(summary)

            with _battle_lock:
                if battle_key in _battle_state:
                    del _battle_state[battle_key]
            logger.info("Battle ended for %s: score=%d/%d", robot.user_name, total_score, max_possible)
        else:
            # ── Bot sends counter-sticker ──
            import os as _os
            import random as _r
            stickerdir = StickerTool.STICKER_DIR
            chosen: str | None = None
            try:
                files = [
                    f for f in _os.listdir(stickerdir)
                    if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                    and f not in battle.get("used_stickers", [])
                ]
                if files:
                    chosen = _r.choice(files)
                else:
                    # All stickers used — pick any
                    all_files = [
                        f for f in _os.listdir(stickerdir)
                        if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
                    ]
                    if all_files:
                        chosen = _r.choice(all_files)
                        battle.setdefault("used_stickers", []).clear()

                if chosen:
                    battle.setdefault("used_stickers", []).append(chosen)
                    from qq_official import MessageBuilder
                    builder = MessageBuilder()
                    builder.image(f"{stickerdir}/{chosen}")
                    builder.text(f"\n{comeback}")
                    robot.send(builder.build())
            except Exception:
                logger.exception("Failed to send counter-sticker in battle")

            # Advance round and refresh timeout
            battle["round"] += 1
            battle["started_at"] = time.time()

    except Exception:
        logger.exception("Battle round failed for %s", robot.user_name)
        robot.reply("呜～斗图出了点问题，不过没关系，继续下一张吧！")
        battle["round"] += 1
        battle["started_at"] = time.time()


def _check_and_handle_battle(robot: RobotServer, battle_key: str, has_images: bool, image_url: str, now: float, disabled: set[str] | None = None) -> bool:
    """Check if this user has an active battle, and handle the incoming message.

    Returns True if the battle consumed this message (caller should return from main_logic).
    Returns False if no battle is active for this user.
    """
    with _battle_lock:
        battle = _battle_state.get(battle_key)
        if not battle or not battle.get("active"):
            return False

        # Feature gate: sticker battle turned off mid-battle → end it now.
        # (Battle-round images are internal to sticker_battle and intentionally
        #  unaffected by the sticker / vision toggles.)
        if disabled and "sticker_battle" in disabled:
            del _battle_state[battle_key]
            robot.reply("本群已关闭斗图功能，本次对战到此结束～(◕‿◕✿)")
            logger.info("Battle ended for %s: sticker_battle disabled", robot.user_name)
            return True

        # Check timeout
        if now - battle.get("started_at", 0) > BATTLE_TIMEOUT:
            battle["active"] = False
            del _battle_state[battle_key]
            robot.reply("斗图超时啦～下次再战吧！(◕‿◕✿)")
            logger.info("Battle timed out for %s", robot.user_name)
            return True

    # Battle is active — determine how to handle this message
    if not has_images:
        # User sent text instead of image → surrender
        total = battle.get("total_score", 0)
        rounds_done = battle["round"] - 1
        if rounds_done > 0:
            robot.reply(
                f"哼！这就认输了吗？坚持了{rounds_done}轮，得分{total}分！\n"
                "下次再战～(◕‿◕✿)"
            )
        else:
            robot.reply("欸？还没开始就认输了？下次准备好了再来哦～(◕‿◕✿)")
        with _battle_lock:
            if battle_key in _battle_state:
                del _battle_state[battle_key]
        logger.info("Battle ended by text for %s after %d rounds", robot.user_name, rounds_done)
        return True

    # User sent an image — process battle round
    logger.info("Battle round %d for %s", battle["round"], robot.user_name)
    _process_battle_round(robot, battle_key, battle, image_url)
    return True


def _clean_expired_battles(now: float) -> None:
    """Remove expired battle states from memory."""
    with _battle_lock:
        expired = [
            k for k, v in _battle_state.items()
            if now - v.get("started_at", 0) > BATTLE_TIMEOUT
        ]
        for k in expired:
            logger.info("Cleaning expired battle: %s", k)
            del _battle_state[k]


def _trigger_profile_update(robot: RobotServer, disabled: set[str] | None = None) -> None:
    """Check if user needs profile analysis and submit if so."""
    if robot.msg_type != "group" or not robot.group_id:
        return
    # Skip bot accounts
    if robot.user_id in Config.BOT_QQ_LIST:
        return
    # Respect per-group feature toggle
    if disabled and "profiles" in disabled:
        return
    try:
        if profile_service.should_analyze(db, robot.user_id, robot.group_id):
            executor.submit(
                profile_service.analyze_user,
                db, robot.user_id, robot.group_id, robot.user_name,
            )
    except Exception:
        logger.debug("main._trigger_profile_update 忽略了异常", exc_info=True)


def _run_ai_judge(
    robot: RobotServer, prev_turn: dict[str, str] | None,
    learning_on: bool, affection_on: bool,
) -> None:
    """Async AI judge: ONE flash call for sentiment + previous-turn learning.
    Silent on failure (no note, no delta) — must never affect the chat path."""
    try:
        if affection_on:
            judge_service.judge_turn(
                db, robot, prev_turn=prev_turn,
                learning_on=learning_on, affection_on=True,
            )
        elif learning_on and prev_turn:
            learning_service.evaluate_prev(db, robot.user_id, prev_turn, robot.msg)
    except Exception:
        logger.exception("AI judge failed for %s", robot.user_id)


def main_logic(robot: RobotServer) -> None:
    try:
        # ── Skip bot's own messages (echo prevention) ──────
        if str(robot.user_id) == (Config.ROBOT_QQ or ""):
            return

        # Record group message (include image presence in content)
        if robot.msg_type == "group" and robot.group_id:
            _seed_group(robot.group_id)
            msg_content = robot.msg.strip()
            if not msg_content and robot.incoming.has_images:
                msg_content = "[图片消息]"
            if msg_content:
                # 官方平台**没有 QQ seq 这个概念**：一条消息只有一个字符串 id
                # （`ROBOT1.0_...`），引用也只给一个索引（`ref_msg_idx`）。
                # 所以 `message_seq` / `reply_to_seq` 一律不传 —— 它们是 OneBot
                # 时代的列，现在**只写不读**，传了也没人看。
                #
                # 这里曾经传的是 `robot.incoming.message_seq` 和
                # `reply.message_seq`，而 `IncomingMessage` / `QuoteInfo` 上
                # **都没有这个属性** → 每条群消息都在这里 `AttributeError`，
                # 用户看到的是「抱歉，处理消息时遇到了问题，请稍后再试~」。
                # 私聊不走这一行，所以症状只在群里出现，很难联想到是记录语句。
                db.record_group_message(
                    robot.group_id, robot.user_id, robot.user_name, msg_content,
                    robot.user_role or "",
                    message_id=robot.incoming.message_id,
                )

        # ── Feature gate: effective scope + disabled features ──
        scope_type, scope_id = feature_gate.scope_of(robot)
        disabled = feature_gate.disabled_keys(scope_type, scope_id)
        vision_on = Config.VISION_ENABLED and "vision" not in disabled

        # ── Sticker understanding flow ────────────────────
        now = time.time()
        pending_key = f"{robot.user_id}:{robot.group_id or 'private'}"
        has_images = robot.incoming.has_images
        first_image_url = robot.incoming.image_urls[0] if robot.incoming.image_urls else ""

        # Clean expired pending requests
        with _sticker_pending_lock:
            expired = [k for k, v in _sticker_pending.items() if now - v > STICKER_REQUEST_TIMEOUT]
            for k in expired:
                del _sticker_pending[k]

        # ── Sticker battle check (highest priority) ──────
        _clean_expired_battles(now)
        if _check_and_handle_battle(robot, pending_key, has_images, first_image_url, now, disabled):
            return

        # ── Pending sticker request armed by the request_sticker AI tool ──
        # Consume ONLY when this message actually carries an image, so an
        # in-between text message does not silently cancel the request.
        # Stale entries are removed by the lazy cleanup above (30s timeout).
        has_pending = False
        with _sticker_pending_lock:
            if pending_key in _sticker_pending and has_images and vision_on:
                has_pending = True
                del _sticker_pending[pending_key]

        if has_pending:
            logger.info("🎯 Sticker flow: pending request consumed, analyzing image from %s", robot.user_name)
            _process_sticker_analysis(robot, first_image_url)
            return

        # ── @bot + image → analyze ──────────────────────
        if robot.at_judgement and robot.msg_type == "group":
            if has_images and vision_on:
                logger.info("🎯 Sticker flow: direct analysis (@bot+image) from %s", robot.user_name)
                _process_sticker_analysis(robot, first_image_url)
                return

            # No image and no text (bare @bot ping / QQ-face-only) → cheap canned
            # greeting. No pending, no AI call.
            if not robot.msg.strip() and not has_images:
                logger.info("🎯 Empty @bot message from %s → canned greeting", robot.user_name)
                robot.reply("我在哦～有什么可以帮你的吗？(｡･ω･｡)")
                return

        # ── Private chat with images — always analyze ──
        if has_images and robot.msg_type == "private" and vision_on:
            logger.info("🎯 Sticker flow: private chat image from %s", robot.user_name)
            _process_sticker_analysis(robot, first_image_url)
            return

        # Only respond to @bot or private (after sticker flow)
        if not robot.at_judgement and robot.msg_type != "private":
            return

        # Skip bot accounts (self + other bots like QQ 小冰)
        if robot.user_id in Config.BOT_QQ_LIST:
            return

        # ── Affection: record valid interaction (base points only, sync) ──
        if robot.msg_type == "group" and robot.group_id and "affection" not in disabled:
            try:
                affection_service.record_interaction(
                    db, robot.user_id, robot.group_id, robot.user_name,
                )
            except Exception:
                logger.debug("main.main_logic 忽略了异常", exc_info=True)

        # ── AI judge (async): sentiment of THIS message + lesson for the previous turn.
        # The pending turn is captured synchronously so the worker always judges the
        # turn that preceded THIS message (no executor-timing race on _pending).
        has_text = bool(robot.msg.strip())
        learning_on = "learning" not in disabled
        affection_on = bool(robot.group_id) and "affection" not in disabled and has_text
        if learning_on or affection_on:
            prev_turn = learning_service.take_pending(robot.user_id) if learning_on else None
            if prev_turn and len(robot.msg.strip()) < learning_service.MIN_MSG_LENGTH:
                prev_turn = None   # very short follow-ups are ignored (same rule as before)
            executor.submit(_run_ai_judge, robot, prev_turn, learning_on, affection_on)

        # Trigger profile analysis for group messages (async, non-blocking)
        _trigger_profile_update(robot, disabled)

        history = _load_history(robot.user_id, robot.group_id)
        is_private = robot.msg_type == "private"
        user_text = _context(robot, _reply_note(robot), mood=_mood_signal(robot))
        system_prompt = _build_system_prompt(
            robot, db, profile_service, learning_service, affection_service, disabled,
        )

        # Every enabled tool is offered — the AI picks via native function calling
        active_tools = _enabled_tools(disabled)

        if is_private:
            logger.info("Private chat with %s (%d tools)", robot.user_name, len(active_tools))
        else:
            logger.info("Group chat with %s (%d tools)", robot.user_name, len(active_tools))

        ai = AiServer(system_prompt, user_text, history, active_tools,
                      model_type=Config.DEEPSEEK_MODEL, thinking_type="enabled")
        ai.group_id = robot.group_id or ""   # metrics attribution
        ai.ai_request()

        # Log thinking chain
        _log_thinking(robot.user_name, ai.reasoning_content)

        tool_calls = ai.ai_message.get("tool_calls") if ai.ai_message else None
        final_text = ""
        if tool_calls:
            tc_list = tool_calls if isinstance(tool_calls, list) else [tool_calls]
            follow_up_tcs: list[dict[str, Any]] = []

            for tc in tc_list:
                fn = tc.get("function", {}).get("name", "")
                handler = ROUTES.get(fn)
                if not handler:
                    continue

                # Affection bonus for tool engagement
                if "affection" not in disabled:
                    try:
                        affection_service.record_tool_usage(
                            db, robot.user_id, robot.group_id, robot.user_name,
                        )
                    except Exception:
                        logger.debug("main.main_logic 忽略了异常", exc_info=True)

                if fn in SELF_CONTAINED_TOOLS:
                    handler(robot, ai)
                else:
                    handler(robot, ai)
                    follow_up_tcs.append(tc)
                    tc_id = tc.get("id", "")
                    result_text = getattr(ai, "tool_result_text", "") or ai.user_text or ""
                    ai.add_tool_result(tc_id, result_text)

                # Recorded after the handler so the chain also carries the
                # outcome — this is what explain_self replays back to the user.
                try:
                    db.record_tool_usage(
                        fn, robot.user_id, robot.group_id,
                        arguments=tc.get("function", {}).get("arguments", ""),
                        result=getattr(ai, "tool_result_text", "") or "",
                        reasoning=ai.reasoning_content or "",
                    )
                except Exception:
                    logger.debug("tool chain record failed", exc_info=True)

            if follow_up_tcs:
                ai.follow_up_request(follow_up_tcs)
                _log_thinking(robot.user_name, ai.reasoning_content)
                final_text = ai.ai_text or ""
        elif ai.ai_text:
            final_text = ai.ai_text

        # Persist BEFORE sending. Delivery can block (slow or failing send),
        # and a user who immediately asks "what were you thinking" must not
        # race an unwritten record.
        # The persona forbids reciting the prompt, but that is just text in a
        # prompt and it failed once under repeated pressure ("好吧好吧，别刷屏
        # 了，贴就贴"). Check the outgoing reply too, and record what was
        # actually sent so the model's own history shows the firm stance.
        if final_text and leaked_persona(final_text):
            logger.warning("Blocked a system-prompt leak: %s", final_text[:150])
            final_text = deflection_for(final_text)

        # Never send nothing. An empty reply reads as "the bot is offline",
        # which is worse than a vague line — and it happens for real: a
        # thinking model can spend its whole budget on reasoning and return no
        # content at all. Self-contained tools have already replied for the
        # turn, so they are exempt.
        handled = bool(tool_calls)
        if not final_text and not handled:
            final_text = filler_for(robot.msg)
            logger.warning("Model returned no text; sending a filler line instead")

        _save_turn(robot.user_id, robot.group_id, robot.msg, final_text,
                   reasoning=ai.reasoning_content or "",
                   tool_chain=_tool_chain_json(tool_calls),
                   # A self-contained tool replied on its own, so this turn is
                   # answered even though final_text stayed empty.
                   handled=handled)
        if final_text:
            robot.reply(final_text)

        # Record turn for learning (evaluated on next user message)
        if "learning" not in disabled:
            tool_name = ""
            if tool_calls:
                names = [tc.get("function", {}).get("name", "") for tc in (tool_calls if isinstance(tool_calls, list) else [tool_calls])]
                tool_name = ",".join(names)
            learning_service.record_turn(robot.user_id, robot.msg, ai.ai_text or "", tool_name)

    except Exception:
        logger.exception("Error for user %s", robot.user_id)
        try: robot.reply("抱歉，处理消息时遇到了问题，请稍后再试~")
        except Exception:
            logger.debug("main.main_logic 忽略了异常", exc_info=True)

# ── HTTP routes ─────────────────────────────────────────
@app.route("/healthz")
def healthz():
    """Unauthenticated liveness probe for the container healthcheck."""
    return jsonify({"ok": True})


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _asset_version() -> str:
    """Cache-buster derived from the dashboard asset mtimes."""
    latest = 0.0
    for rel in ("css/app.css", "js/app.js"):
        try:
            latest = max(latest, os.path.getmtime(os.path.join(_STATIC_DIR, rel)))
        except OSError:
            logger.debug("main._asset_version 忽略了异常", exc_info=True)
    return str(int(latest)) or "1"


@app.route("/", methods=["GET"])
def dashboard():
    return render_template("dashboard.html", asset_v=_asset_version())

@app.route("/status")
def status():
    # Count stickers
    sticker_count = 0
    try:
        sticker_count = len([f for f in os.listdir(STICKER_DIR) if os.path.isfile(os.path.join(STICKER_DIR, f))])
    except Exception:
        logger.debug("main.status 忽略了异常", exc_info=True)
    uptime_sec = int(time.time() - _start_time)
    return jsonify({"ok": True, "model": Config.DEEPSEEK_MODEL, "thinking": "enabled",
                    "tools": len(tools_def.ai_tools()), "groups": len(_seeded_groups),
                    "uptime": uptime_sec,
                    "app_id": Config.QQ_APP_ID,
                    "gateway": gateway.status if gateway else None,
                    "scheduler": scheduler._running, "stickers": sticker_count})

@app.route("/stream")
def stream():
    def gen():
        while True:
            entries = sse_handler.read()
            if entries: yield f"data: {json.dumps(entries, ensure_ascii=False)}\n\n"
            else: yield ": keepalive\n\n"
            time.sleep(0.5)
    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/stats")
def api_stats():
    return jsonify({"totals": db.get_total_stats(),
                    "tools": [{"name": r[0], "count": r[1]} for r in db.get_tool_stats()]})

@app.route("/api/tarot")
def api_tarot(): return jsonify({"records": db.get_all_tarot_history(50)})

@app.route("/api/history")
def api_history(): return jsonify({"records": db.get_all_history(50)})

@app.route("/api/profiles")
def api_profiles(): return jsonify({"profiles": db.get_all_profiles()})

@app.route("/api/learning")
def api_learning():
    rows = db.fetch_data(
        "SELECT id, user_id, note, user_msg, ai_text, tool_name, timestamp FROM learning_log ORDER BY id DESC LIMIT 100"
    )
    return jsonify({"notes": [
        {
            "id": r[0], "user_id": r[1], "note": r[2],
            "user_msg": r[3] or "", "ai_text": r[4] or "",
            "tool_name": r[5] or "", "time": r[6],
        }
        for r in rows
    ]})

@app.route("/api/learning", methods=["POST"])
def api_learning_create():
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "dashboard").strip()
    note = (data.get("note") or "").strip()
    if not note:
        return jsonify({"ok": False, "error": "笔记内容不能为空"}), 400
    tool_name = (data.get("tool_name") or "").strip()
    user_msg = (data.get("user_msg") or "").strip()
    ai_text = (data.get("ai_text") or "").strip()
    try:
        db.execute_action(
            "INSERT INTO learning_log (user_id, note, tool_name, user_msg, ai_text) VALUES (?, ?, ?, ?, ?)",
            (user_id, note, tool_name, user_msg, ai_text),
        )
        return jsonify({"ok": True, "msg": "学习笔记已添加"})
    except Exception:
        logger.exception("Failed to create learning note")
        return jsonify({"ok": False, "error": "数据库写入失败"}), 500

@app.route("/api/learning/<int:note_id>", methods=["DELETE"])
def api_learning_delete(note_id: int):
    try:
        db.execute_action("DELETE FROM learning_log WHERE id = ?", (note_id,))
        return jsonify({"ok": True, "deleted": note_id})
    except Exception:
        logger.exception("Failed to delete learning note #%d", note_id)
        return jsonify({"ok": False, "error": "数据库删除失败"}), 500

# ── Affection API ─────────────────────────────────

@app.route("/api/affection")
def api_affection():
    """Get all affection records, optionally filtered by group."""
    group_id = request.args.get("group_id", "")
    rows = db.fetch_data(
        "SELECT user_id, group_id, user_name, affection_score, interaction_count, "
        "positive_count, negative_count, last_interaction, relationship, notes "
        "FROM user_affection ORDER BY affection_score DESC LIMIT 200"
    )
    records = []
    for r in rows:
        if group_id and r[1] != group_id:
            continue
        label, emoji = AffectionService.get_relationship(float(r[3]))
        records.append({
            "user_id": r[0], "group_id": r[1], "user_name": r[2],
            "affection_score": round(float(r[3]), 1),
            "interaction_count": int(r[4]),
            "positive_count": int(r[5]), "negative_count": int(r[6]),
            "last_interaction": r[7], "relationship": r[8] or label,
            "emoji": emoji, "notes": r[9] or "",
        })
    return jsonify({"affection_records": records, "total": len(records)})


@app.route("/api/affection/leaderboard")
def api_affection_leaderboard():
    """Get affection leaderboard, optionally filtered by group."""
    group_id = request.args.get("group_id", "")
    limit = request.args.get("limit", 20, type=int)
    board = AffectionService.get_leaderboard(db, group_id=group_id or None, limit=limit)
    return jsonify({"leaderboard": board, "group_id": group_id or None})


@app.route("/api/affection/<user_id>")
def api_affection_user(user_id: str):
    """Get affection details for a specific user."""
    group_id = request.args.get("group_id", "")
    if not group_id:
        return jsonify({"ok": False, "error": "group_id query parameter is required"}), 400
    record = AffectionService.get_or_create(db, user_id, group_id, user_id)
    label, emoji = AffectionService.get_relationship(record["affection_score"])
    return jsonify({
        "user_id": user_id,
        "group_id": group_id,
        "affection_score": round(record["affection_score"], 1),
        "interaction_count": record["interaction_count"],
        "positive_count": record["positive_count"],
        "negative_count": record["negative_count"],
        "last_interaction": record["last_interaction"],
        "relationship": label,
        "emoji": emoji,
        "notes": record.get("notes", ""),
    })


@app.route("/api/affection/adjust", methods=["POST"])
def api_affection_adjust():
    """Manually adjust affection score (dashboard use)."""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip()
    group_id = (data.get("group_id") or "").strip()
    delta = float(data.get("delta", 0))
    note = (data.get("note") or "").strip()
    if not user_id or not group_id:
        return jsonify({"ok": False, "error": "user_id and group_id are required"}), 400
    if delta == 0:
        return jsonify({"ok": False, "error": "delta must be non-zero"}), 400
    try:
        result = AffectionService.manual_adjust(db, user_id, group_id, delta, note)
        return jsonify({"ok": True, "adjusted": result})
    except Exception:
        logger.exception("Failed to adjust affection for %s/%s", user_id, group_id)
        return jsonify({"ok": False, "error": "数据库更新失败"}), 500


@app.route("/api/features")
def api_features():
    rows = db.fetch_data(
        "SELECT id, user_name, request_text, category, priority, status, ai_summary, timestamp "
        "FROM feature_requests ORDER BY id DESC LIMIT 100"
    )
    return jsonify({"features": [
        {"id": r[0], "user_name": r[1], "request": r[2], "category": r[3],
         "priority": r[4], "status": r[5], "summary": r[6], "time": r[7]}
        for r in rows
    ]})

@app.route("/api/features/<int:feature_id>", methods=["PATCH"])
def api_features_update(feature_id: int):
    data = request.get_json(silent=True) or {}
    allowed_fields = {"status", "priority", "category"}
    updates = {k: v for k, v in data.items() if k in allowed_fields and v}
    if not updates:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    valid_statuses = {"pending", "done", "rejected"}
    if "status" in updates and updates["status"] not in valid_statuses:
        return jsonify({"ok": False, "error": f"Invalid status. Must be one of: {valid_statuses}"}), 400
    # Check current status before updating (to prevent duplicate changelog entries)
    old_status = ""
    if updates.get("status") == "done":
        old_rows = db.fetch_data(
            "SELECT status FROM feature_requests WHERE id = ?", (feature_id,)
        )
        if old_rows:
            old_status = old_rows[0][0]
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [feature_id]
    try:
        db.execute_action(f"UPDATE feature_requests SET {set_clause} WHERE id = ?", tuple(values))
        # Auto-add changelog entry when a feature is newly marked as done (not re-done)
        if updates.get("status") == "done" and old_status != "done":
            fr_rows = db.fetch_data(
                "SELECT request_text, ai_summary, user_name FROM feature_requests WHERE id = ?",
                (feature_id,),
            )
            if fr_rows:
                fr_request, fr_summary, fr_user = fr_rows[0]
                version_manager.auto_changelog_for_feature(fr_request, fr_summary, fr_user)
        return jsonify({"ok": True, "updated": updates})
    except Exception:
        logger.exception("Failed to update feature #%d", feature_id)
        return jsonify({"ok": False, "error": "Database update failed"}), 500

@app.route("/api/features/<int:feature_id>", methods=["DELETE"])
def api_features_delete(feature_id: int):
    try:
        db.execute_action("DELETE FROM feature_requests WHERE id = ?", (feature_id,))
        return jsonify({"ok": True, "deleted": feature_id})
    except Exception:
        logger.exception("Failed to delete feature #%d", feature_id)
        return jsonify({"ok": False, "error": "Database delete failed"}), 500

@app.route("/api/features", methods=["POST"])
def api_features_create():
    data = request.get_json(silent=True) or {}
    request_text = (data.get("request") or "").strip()
    if not request_text:
        return jsonify({"ok": False, "error": "Request text is required"}), 400
    category = data.get("category", "未分类")
    priority = data.get("priority", "medium")
    user_name = data.get("user_name", "dashboard")
    user_id = data.get("user_id", "admin")
    try:
        db.deposit(
            "feature_requests",
            "(user_id, user_name, group_id, request_text, category, priority, status, ai_summary)",
            "(?, ?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, user_name, None, request_text, category, priority, request_text[:20]),
        )
        return jsonify({"ok": True, "created": {"request": request_text, "category": category, "priority": priority}})
    except Exception:
        logger.exception("Failed to create feature request")
        return jsonify({"ok": False, "error": "Database insert failed"}), 500

# ── Version & Changelog API ──────────────────────────

@app.route("/api/versions/bump", methods=["POST"])
def api_versions_bump():
    """Bump version number and return the new version string (does not create it)."""
    data = request.get_json(silent=True) or {}
    bump_type = data.get("type", "patch")
    if bump_type not in ("major", "minor", "patch"):
        return jsonify({"ok": False, "error": "type must be: major, minor, or patch"}), 400
    try:
        new_version = version_manager.bump_version(bump_type)
        return jsonify({"ok": True, "version": new_version, "bump": bump_type})
    except Exception:
        logger.exception("Version bump failed")
        return jsonify({"ok": False, "error": "版本号递增失败"}), 500

@app.route("/api/versions/current")
def api_versions_current():
    current = version_manager.get_current_version()
    if not current:
        return jsonify({"ok": False, "error": "No versions found"}), 404
    version_id = current["id"]
    changelogs = version_manager.get_changelogs(version_id=version_id)
    current["changelogs"] = changelogs
    return jsonify({"ok": True, "version": current})

@app.route("/api/versions")
def api_versions():
    versions = version_manager.get_all_versions()
    return jsonify({"ok": True, "versions": versions})

@app.route("/api/versions/<int:version_id>")
def api_version_detail(version_id: int):
    detail = version_manager.get_version_detail(version_id)
    if not detail:
        return jsonify({"ok": False, "error": "Version not found"}), 404
    return jsonify({"ok": True, "version": detail})

@app.route("/api/versions", methods=["POST"])
def api_versions_create():
    data = request.get_json(silent=True) or {}
    version = (data.get("version") or "").strip()
    if not version:
        return jsonify({"ok": False, "error": "Version string is required"}), 400
    # Validate version format: X.Y.Z
    import re
    if not re.match(r"^\d+\.\d+\.\d+$", version):
        return jsonify({"ok": False, "error": "Version must be in format X.Y.Z (e.g. 1.0.0)"}), 400
    description = data.get("description", "")
    author = data.get("author", "dashboard")
    try:
        result = version_manager.create_version(version, description, author)
        return jsonify({"ok": True, "version": result})
    except Exception:
        logger.exception("Failed to create version %s", version)
        return jsonify({"ok": False, "error": "Database insert failed"}), 500

@app.route("/api/changelog")
def api_changelog():
    version_id = request.args.get("version_id", type=int)
    entry_type = request.args.get("type")
    limit = request.args.get("limit", 100, type=int)
    entries = version_manager.get_changelogs(version_id=version_id, entry_type=entry_type, limit=limit)
    return jsonify({"ok": True, "changelogs": entries})

@app.route("/api/changelog", methods=["POST"])
def api_changelog_create():
    data = request.get_json(silent=True) or {}
    version_id = data.get("version_id", 0)
    entry_type = data.get("entry_type", "feature")
    title = (data.get("title") or "").strip()
    description = data.get("description", "")
    author = data.get("author", "dashboard")
    if not version_id or not title:
        return jsonify({"ok": False, "error": "version_id and title are required"}), 400
    if entry_type not in {"feature", "fix", "improve", "breaking"}:
        return jsonify({"ok": False, "error": "entry_type must be one of: feature, fix, improve, breaking"}), 400
    try:
        entry = version_manager.add_changelog(version_id, entry_type, title, description, author)
        return jsonify({"ok": True, "entry": entry})
    except Exception:
        logger.exception("Failed to create changelog entry")
        return jsonify({"ok": False, "error": "Database insert failed"}), 500

@app.route("/api/messages")
def api_messages():
    rows = db.get_recent_group_messages("", 100)
    return jsonify({"records": [{"user_id": r[0], "user_name": r[1], "content": r[2][:100], "time": r[3]} for r in rows]})

@app.route("/api/balance")
def api_balance():
    result = balance_service.get_balance()
    return jsonify(result)

@app.route("/api/scheduler")
def api_scheduler():
    """Background scheduler status.

    提醒 / 早间问候 / 群推送订阅都随官方平台的主动推送一起删除了，
    现在的调度器只剩每日的保留策略与数据库备份，所以不再有「下次早安」
    之类的时间点信息。
    """
    return jsonify({"running": scheduler._running,
                    "check_interval": scheduler.CHECK_INTERVAL})

@app.route("/api/stickers")
def api_stickers():
    category = request.args.get("category", "")
    stickers = []
    try:
        stickers = db.get_stickers(category)
    except Exception:
        logger.debug("main.api_stickers 忽略了异常", exc_info=True)
    # If no DB records, fall back to file scan
    if not stickers:
        try:
            for f in sorted(os.listdir(STICKER_DIR)):
                fpath = os.path.join(STICKER_DIR, f)
                if os.path.isfile(fpath):
                    size = os.path.getsize(fpath)
                    stickers.append({
                        "filename": f, "file_hash": "", "category": "未分类",
                        "content_desc": "", "emotion": "", "file_size": size,
                        "collected_at": "", "url": f"/stickers/{f}",
                    })
        except Exception:
            logger.debug("main.api_stickers 忽略了异常", exc_info=True)
    # Add URL to each sticker
    for s in stickers:
        s["url"] = f"/stickers/{s.get('filename', '')}"
    return jsonify({"stickers": stickers, "total": len(stickers)})

@app.route("/api/stickers/categories")
def api_stickers_categories():
    try:
        counts = db.count_stickers_by_category()
        return jsonify({"categories": [{"name": r[0], "count": r[1]} for r in counts]})
    except Exception:
        return jsonify({"categories": []})

@app.route("/api/stickers/<filename>/category", methods=["PATCH"])
def api_stickers_update_category(filename: str):
    data = request.get_json(silent=True) or {}
    category = data.get("category", "").strip()
    if not category:
        return jsonify({"ok": False, "error": "Category is required"}), 400
    if category not in STICKER_CATEGORIES:
        return jsonify({"ok": False, "error": f"无效分类。可选: {', '.join(sorted(STICKER_CATEGORIES))}"}), 400
    content_desc = data.get("content_desc", "")
    emotion = data.get("emotion", "")
    try:
        db.update_sticker_category(filename, category, content_desc, emotion)
        logger.info("Sticker %s category updated to %s", filename, category)
        return jsonify({"ok": True, "updated": {"filename": filename, "category": category}})
    except Exception:
        logger.exception("Failed to update sticker category: %s", filename)
        return jsonify({"ok": False, "error": "数据库更新失败"}), 500

@app.route("/api/stickers/<filename>", methods=["DELETE"])
def api_stickers_delete(filename: str):
    """Delete a sticker file and its DB entry."""
    fpath = os.path.join(STICKER_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"ok": False, "error": "文件不存在"}), 404
    try:
        os.remove(fpath)
        # Remove from DB
        try:
            db.execute_action("DELETE FROM stickers WHERE filename = ?", (filename,))
        except Exception:
            logger.debug("main.api_stickers_delete 忽略了异常", exc_info=True)
        # Invalidate sticker collector caches
        sticker_collector._hashes = None
        sticker_collector._phashes = None
        logger.info("Sticker deleted: %s", filename)
        return jsonify({"ok": True, "deleted": filename})
    except Exception:
        logger.exception("Failed to delete sticker: %s", filename)
        return jsonify({"ok": False, "error": "文件删除失败"}), 500

@app.route("/api/stickers/orphans/cleanup", methods=["POST"])
def api_stickers_orphans_cleanup():
    """Remove DB entries for stickers whose files no longer exist."""
    try:
        count = db.cleanup_orphan_stickers(STICKER_DIR)
        return jsonify({"ok": True, "cleaned": count})
    except Exception:
        logger.exception("Failed to clean orphan stickers")
        return jsonify({"ok": False, "error": "清理失败"}), 500

# ── Batch sticker organize ────────────────────────────

_sticker_organize_state: dict[str, Any] = {
    "running": False,
    "total": 0,
    "completed": 0,
    "failed": 0,
    "errors": [],
    "started_at": None,
}

@app.route("/api/stickers/organize", methods=["POST"])
def api_stickers_organize():
    """Trigger batch categorization of all uncategorized stickers."""
    if _sticker_organize_state["running"]:
        return jsonify({"ok": False, "error": "批量分类已在运行中"}), 400
    executor.submit(_batch_categorize_stickers)
    return jsonify({"ok": True, "msg": "批量分类已启动"})

@app.route("/api/stickers/organize/progress")
def api_stickers_organize_progress():
    """Return batch categorization progress."""
    return jsonify(_sticker_organize_state)

@app.route("/api/stickers/organize", methods=["DELETE"])
def api_stickers_organize_cancel():
    _sticker_organize_state["running"] = False
    return jsonify({"ok": True, "msg": "分类已取消"})

def _batch_categorize_stickers():
    """Background task: categorize all uncategorized stickers using vision API.

    Calls vision_analyze_with_category() for each uncategorized sticker
    and updates the DB with description, emotion, and category.
    Supports cancellation via _sticker_organize_state["running"].
    """
    _sticker_organize_state["running"] = True
    _sticker_organize_state["completed"] = 0
    _sticker_organize_state["failed"] = 0
    _sticker_organize_state["errors"] = []
    _sticker_organize_state["started_at"] = time.time()

    try:
        # Get all uncategorized stickers from DB
        uncategorized = db.get_uncategorized_stickers()
        _sticker_organize_state["total"] = len(uncategorized)
        logger.info("Batch categorize: %d uncategorized stickers found", len(uncategorized))

        for fname, file_hash in uncategorized:
            if not _sticker_organize_state["running"]:
                break

            fpath = os.path.join(STICKER_DIR, fname)
            if not os.path.isfile(fpath):
                _sticker_organize_state["failed"] += 1
                _sticker_organize_state["errors"].append(f"{fname}: file not found")
                continue

            try:
                vision_data = AiServer.vision_analyze_with_category(fpath)
                if vision_data:
                    db.update_sticker_category(
                        fname,
                        vision_data.get("category", "其他"),
                        vision_data.get("description", ""),
                        vision_data.get("emotion", ""),
                    )
                    _sticker_organize_state["completed"] += 1
                else:
                    # Vision API returned None — leave as uncategorized
                    db.update_sticker_category(fname, "未分类", "", "")
                    _sticker_organize_state["completed"] += 1
            except Exception as e:
                _sticker_organize_state["failed"] += 1
                _sticker_organize_state["errors"].append(f"{fname}: {str(e)[:80]}")

            # Rate limit: 0.5s between vision API calls
            time.sleep(0.5)

            done = _sticker_organize_state["completed"] + _sticker_organize_state["failed"]
            if done % 5 == 0:
                logger.info("Batch categorize: %d/%d (failed: %d)",
                             _sticker_organize_state["completed"],
                             _sticker_organize_state["total"],
                             _sticker_organize_state["failed"])

        duration = int(time.time() - (_sticker_organize_state["started_at"] or time.time()))
        logger.info("Batch categorize finished: %d categorized, %d failed in %ds",
                     _sticker_organize_state["completed"],
                     _sticker_organize_state["failed"], duration)
    finally:
        _sticker_organize_state["running"] = False

# ── Sticker dedup endpoints ──────────────────────────

_sticker_dedup_state: dict[str, Any] = {
    "running": False,
    "scan_result": None,
    "cleanup_result": None,
}

@app.route("/api/stickers/duplicates")
def api_stickers_duplicates():
    """Scan for visually similar duplicate stickers using perceptual hash."""
    force = request.args.get("force", "0") == "1"
    if _sticker_dedup_state["running"] and not force:
        return jsonify({"ok": False, "error": "扫描已在运行中"}), 400

    executor.submit(_scan_duplicates)
    return jsonify({"ok": True, "msg": "重复扫描已启动"})

@app.route("/api/stickers/duplicates/progress")
def api_stickers_duplicates_progress():
    """Return duplicate scan results."""
    return jsonify({
        "running": _sticker_dedup_state["running"],
        "scan_result": _sticker_dedup_state["scan_result"],
        "cleanup_result": _sticker_dedup_state["cleanup_result"],
    })

@app.route("/api/stickers/duplicates/cleanup", methods=["POST"])
def api_stickers_duplicates_cleanup():
    """Remove duplicate stickers, keeping the best quality one from each group."""
    if _sticker_dedup_state["running"]:
        return jsonify({"ok": False, "error": "请等待当前操作完成"}), 400

    dry_run = request.args.get("dry_run", "1") == "1"
    executor.submit(_cleanup_duplicates, dry_run)
    return jsonify({"ok": True, "msg": f"去重清理已启动（{'预览模式' if dry_run else '执行模式'}）"})

def _scan_duplicates():
    """Background task: scan for visually similar stickers."""
    _sticker_dedup_state["running"] = True
    _sticker_dedup_state["scan_result"] = None
    try:
        groups = sticker_collector.find_duplicates()
        result = []
        for group in groups:
            group_info = []
            for f in group:
                group_info.append({
                    "filename": f["filename"],
                    "file_size": f["file_size"],
                    "phash": f.get("phash", ""),
                })
            result.append(group_info)

        total_dups = sum(len(g) - 1 for g in groups)
        waste_bytes = sum(
            sum(f["file_size"] for f in g[1:]) for g in groups
        )
        _sticker_dedup_state["scan_result"] = {
            "total_stickers": len(sticker_collector.hashes),
            "groups": len(groups),
            "duplicate_files": total_dups,
            "waste_bytes": waste_bytes,
            "details": result,
        }
        logger.info("Duplicate scan complete: %d groups, %d duplicates, %d bytes wasted",
                     len(groups), total_dups, waste_bytes)
    except Exception:
        logger.exception("Duplicate scan failed")
        _sticker_dedup_state["scan_result"] = {"error": "扫描失败"}
    finally:
        _sticker_dedup_state["running"] = False

def _cleanup_duplicates(dry_run: bool):
    """Background task: remove duplicate stickers."""
    _sticker_dedup_state["running"] = True
    _sticker_dedup_state["cleanup_result"] = None
    try:
        result = sticker_collector.cleanup_duplicates(dry_run=dry_run)
        _sticker_dedup_state["cleanup_result"] = result
        logger.info(
            "Duplicate cleanup (%s): %d groups, %d removed, %d kept, %d bytes freed",
            "dry_run" if dry_run else "executed",
            result["groups_cleaned"], result["files_removed"],
            result["files_kept"], result["total_waste_bytes"],
        )
    except Exception:
        logger.exception("Duplicate cleanup failed")
        _sticker_dedup_state["cleanup_result"] = {"error": "清理失败"}
    finally:
        _sticker_dedup_state["running"] = False

def _list_groups() -> list[dict]:
    """All groups that ever spoke, with message stats (shared by /api/groups and settings)."""
    groups = []
    try:
        rows = db.fetch_data(
            "SELECT group_id, COUNT(*), MAX(timestamp) FROM group_messages "
            "WHERE group_id IS NOT NULL GROUP BY group_id"
        )
    except Exception:
        rows = []
    for gid, msg_count, last_active in rows:
        # Get group name from cache or API
        gname = ""
        try:
            info = client.get_group_info(gid)
            gname = info.get("group_name", "") if info else ""
        except Exception:
            logger.debug("main._list_groups 忽略了异常", exc_info=True)
        groups.append({"group_id": gid, "group_name": gname or gid,
                       "msg_count": msg_count, "last_active": last_active or ""})
    groups.sort(key=lambda g: g["msg_count"], reverse=True)
    return groups

@app.route("/api/groups")
def api_groups():
    groups = _list_groups()
    return jsonify({"groups": groups, "total": len(groups)})


@app.route("/api/groups/<group_id>/purge-preview")
def api_group_purge_preview(group_id: str):
    """What deleting this group would remove — shown in the confirm dialog."""
    return jsonify({"ok": True, "group_id": group_id,
                    "counts": db.group_purge_preview(group_id)})


@app.route("/api/groups/<group_id>", methods=["DELETE"])
def api_group_delete(group_id: str):
    """Remove a group: purge all of its data, optionally make the bot leave.

    Body: {"leave": true} also makes the bot exit the group through the
    official API. Leaving is not done implicitly — it cannot be undone from
    here, the bot must be re-invited.
    """
    body = request.get_json(silent=True) or {}
    leave = bool(body.get("leave"))
    counts = db.purge_group(group_id)
    left = False
    leave_error = ""
    if leave:
        try:
            # 官方平台的退群接口。这里以前调的是 OneBot 的
            # `client.call("set_group_leave", ...)`，而 QQOfficialClient 根本没有
            # `call()` —— AttributeError 被下面的 except 吞掉，于是「删除群聊并让
            # 机器人退群」会静默地只删数据、不退群。改用官方客户端的 leave_group()。
            left = bool(client.leave_group(str(group_id)))
            if not left:
                leave_error = "官方接口退群失败"
        except Exception as exc:
            leave_error = str(exc)
            logger.exception("Failed to leave group %s", group_id)
    logger.info("Group %s deleted (leave=%s, left=%s)", group_id, leave, left)
    return jsonify({"ok": True, "group_id": group_id, "deleted": counts,
                    "left": left, "leave_error": leave_error})


# 群推送订阅的三个路由（GET/POST/DELETE /api/subscriptions）已随主动推送一起
# 删除：订阅靠「到点主动往群里发消息」，而官方平台 2025-04-21 起没有主动推送，
# 消费它的调度器逻辑 _check_subscriptions 也已移除，留着只会是一组永远不生效的接口。
# 表 `group_subscriptions` 保留在库里（不 DROP），因为 purge_group 的表清单里还
# 列着它，旧库也有这张表。


@app.route("/api/amp-heads")
def api_amp_heads():
    """The amp-head library: hand-written rows plus anything crawled."""
    try:
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        limit, offset = 200, 0
    return jsonify({
        "ok": True,
        "heads": db.get_amp_heads(limit, offset),
        "total": db.count_amp_heads(),
        "manual": db.count_amp_heads("manual"),
        "crawled": db.count_amp_heads("wikipedia"),
        "last_crawl": db.get_state("amp_crawl_last") or "",
        "crawl_running": db.get_state("amp_crawl_running") == "1",
    })


@app.route("/api/amp-heads/<int:head_id>", methods=["DELETE"])
def api_amp_head_delete(head_id: int):
    try:
        db.delete_amp_head(head_id)
        return jsonify({"ok": True, "deleted": head_id})
    except Exception:
        logger.exception("Failed to delete amp head #%d", head_id)
        return jsonify({"ok": False, "error": "Database delete failed"}), 500


@app.route("/api/ai/metrics")
def api_ai_metrics():
    """AI usage over the last N hours: volume, latency, tokens, cost, errors."""
    try:
        hours = int(request.args.get("hours", 24))
    except ValueError:
        hours = 24
    return jsonify({"ok": True, **db.get_ai_metrics(hours)})


# ── Feature settings (per-group / per-user toggles) ─────

@app.route("/api/settings/groups")
def api_settings_groups():
    groups = []
    for g in _list_groups():
        groups.append({**g, "disabled": sorted(feature_gate.disabled_keys("group", g["group_id"]))})
    return jsonify({"ok": True, "features": FEATURE_DEFS, "groups": groups})

@app.route("/api/settings/<scope_type>/<scope_id>")
def api_settings_get(scope_type: str, scope_id: str):
    if scope_type not in VALID_SCOPES:
        return jsonify({"ok": False, "error": "invalid scope_type"}), 400
    return jsonify({"ok": True, "settings": feature_gate.effective_map(scope_type, scope_id)})

@app.route("/api/settings/<scope_type>/<scope_id>", methods=["PATCH"])
def api_settings_patch(scope_type: str, scope_id: str):
    if scope_type not in VALID_SCOPES:
        return jsonify({"ok": False, "error": "invalid scope_type"}), 400
    data = request.json or {}
    key = str(data.get("key", ""))
    enabled = bool(data.get("enabled", True))
    try:
        settings = feature_gate.set_enabled(scope_type, scope_id, key, enabled)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception:
        logger.exception("Failed to update feature settings %s:%s", scope_type, scope_id)
        return jsonify({"ok": False, "error": "failed to save"}), 500
    return jsonify({"ok": True, "settings": settings})

@app.route("/api/settings/<scope_type>/<scope_id>", methods=["DELETE"])
def api_settings_delete(scope_type: str, scope_id: str):
    if scope_type not in VALID_SCOPES:
        return jsonify({"ok": False, "error": "invalid scope_type"}), 400
    try:
        settings = feature_gate.reset(scope_type, scope_id)
    except Exception:
        logger.exception("Failed to reset feature settings %s:%s", scope_type, scope_id)
        return jsonify({"ok": False, "error": "failed to reset"}), 500
    return jsonify({"ok": True, "settings": settings})

@app.route("/stickers/<path:filename>")
def serve_sticker(filename: str):
    return send_from_directory(STICKER_DIR, filename)


def _on_gateway_event(event: dict[str, Any]) -> None:
    """官方 WebSocket 事件 → RobotServer → 业务逻辑。

    原来这里是 Flask 的 `/webhook` 路由（LLBot 主动推给我们）。
    官方平台方向相反，事件从我们维持的 WS 长连接里出来，所以改成
    由网关线程回调到这里。**校验签名那套也随之作废** ——
    连接本身已经用 access_token 认证过了。
    """
    try:
        robot = RobotServer(event, client, Config.ROBOT_QQ or "")
    except Exception:
        logger.exception("事件解析失败：%s", str(event)[:160])
        return
    executor.submit(main_logic, robot)


gateway = GatewayClient(lambda: client.access_token, _on_gateway_event)
gateway.start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
