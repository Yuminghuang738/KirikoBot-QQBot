"""QQ 官方机器人平台客户端。

取代上游分线的 OneBot 客户端（`llbot_client.py`，走 LLBot / NapCat + 一个本地
登录着的 QQ 客户端），那个文件在本分支已经删除。平台相关的代码
**全部集中在这一个文件里**，其余业务代码只调这里的方法，不碰 HTTP/WS 细节。

## 与 OneBot 的三个根本差异

1. **认证**：AppID + AppSecret 换 `access_token`，约 2 小时过期，需自动续期。
   不再是固定的 Bearer token。
2. **身份是 openid，不是 QQ 号**。实测 `id` / `member_openid` / `union_openid`
   三者恒等，且没有 `union_user_account` —— **没有任何能对应回 QQ 号的东西**。
   所以库里以 QQ 号为键的用户数据无法映射（只有群事件里的 `username` 是真昵称）。
3. **只能被动回复**。主动推送已于 2025-04-21 由官方下线；被动回复必须带原消息
   的 `id` 作为 `msg_id`，5 分钟内有效，同一 `msg_id`+`msg_seq` 只能发一次。

## 已实测确认（见 docs/phase0-findings.md）

- access_token 签发、`/users/@me`、`/gateway` 均正常
- WebSocket 收到 `READY` / `GROUP_AT_MESSAGE_CREATE` / `GROUP_ADD_ROBOT`
- 单聊与群聊的被动回复都返回 `HTTP 200` 并给出 message id
- 群事件的 `author.username` 有值（单聊事件没有这个字段）

## 未实测、按文档实现的部分（失败一律降级，不抛到业务层）

- 图片发送需要先走富媒体上传（`file_data` base64 或公网 `url`），
  这里用 base64 并限制大小
- `get_group_info` / `get_group_member_list` 走官方群管理接口；
  官方未必提供成员列表，取不到时返回空
- `leave_group` 走群管理的退出接口
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# 官方富媒体的 base64 上限。超过就放弃发图而不是把整个请求搞失败。
MAX_INLINE_IMAGE = 2 * 1024 * 1024

# 群聊/单聊消息类型
MSG_TYPE_TEXT = 0
MSG_TYPE_MEDIA = 7


# ══════════════════════════════════════════════════════════
#  消息段构造（沿用原来的写法，业务代码不用改）
# ══════════════════════════════════════════════════════════

class MessageBuilder:
    """构造一条待发消息的段列表。

    刻意保留与 OneBot 版同名的接口（`text` / `image` / `at` / `reply` /
    `build`），这样业务代码里那 ~40 处 `MessageBuilder().text(...).build()`
    一行都不用改；差异在客户端里翻译。
    """

    def __init__(self) -> None:
        self._segments: list[dict[str, Any]] = []

    def text(self, content: str) -> "MessageBuilder":
        if content:
            self._segments.append({"type": "text", "data": {"text": content}})
        return self

    def image(self, path: str) -> "MessageBuilder":
        self._segments.append({"type": "image", "data": {"file": path}})
        return self

    def at(self, qq: str) -> "MessageBuilder":
        # 官方平台不允许任意 @ 群成员，`at` 只作记录；发送时会被忽略。
        self._segments.append({"type": "at", "data": {"qq": qq}})
        return self

    def reply(self, message_id: Any) -> "MessageBuilder":
        self._segments.append({"type": "reply", "data": {"id": str(message_id)}})
        return self

    def build(self) -> list[dict[str, Any]]:
        return self._segments


def _plain_text(segments: list[dict[str, Any]]) -> str:
    """把段列表里的文本拼起来 —— 官方接口只接受纯文本 content。"""
    out = ""
    for seg in segments or []:
        if isinstance(seg, dict) and seg.get("type") == "text":
            out += str((seg.get("data") or {}).get("text") or "")
    return out.strip()


def _first_image(segments: list[dict[str, Any]]) -> str:
    for seg in segments or []:
        if isinstance(seg, dict) and seg.get("type") == "image":
            return str((seg.get("data") or {}).get("file") or "")
    return ""


# ══════════════════════════════════════════════════════════
#  收进来的消息
# ══════════════════════════════════════════════════════════

@dataclass
class QuoteInfo:
    """被引用的消息。官方在发送侧「暂未支持」引用，但**收得到**引用信息。"""

    text: str = ""
    sender_name: str = ""
    message_id: str = ""


@dataclass
class IncomingMessage:
    """一条收进来的消息，字段与 OneBot 版保持一致，业务代码无感。"""

    msg_type: str = "private"          # group / private
    user_id: str = ""                  # openid
    user_name: str = ""
    group_id: str = ""                 # group_openid
    group_name: str = ""
    text: str = ""
    message_id: str = ""               # 被动回复要拿它当 msg_id
    reply: QuoteInfo | None = None
    image_urls: list[str] = field(default_factory=list)
    is_at_bot: bool = False
    user_role: str = ""

    @property
    def has_images(self) -> bool:
        return bool(self.image_urls)

    @classmethod
    def from_event(cls, event: dict[str, Any], bot_id: str = "") -> "IncomingMessage":
        """官方事件 → 内部消息对象。

        群事件带 `group_openid` 与 `member_openid`；单聊只有 `user_openid`。
        `content` 已经去掉了 @ 机器人的前缀（官方行为）。
        """
        d = event.get("d") or {}
        author = d.get("author") or {}
        group_id = str(d.get("group_openid") or "")

        images: list[str] = []
        for att in d.get("attachments") or []:
            if not isinstance(att, dict):
                continue
            ctype = str(att.get("content_type") or "")
            if ctype.startswith("image/") and att.get("url"):
                images.append(str(att["url"]))

        # 引用的消息藏在 msg_elements 里（message_type=103）。
        quote = None
        for el in d.get("msg_elements") or []:
            if isinstance(el, dict) and el.get("message_type") == 103:
                qa = el.get("author") or {}
                quote = QuoteInfo(
                    text=str(el.get("content") or ""),
                    sender_name=str(qa.get("username") or ""),
                    message_id=str(el.get("msg_idx") or ""),
                )
                break

        return cls(
            msg_type="group" if group_id else "private",
            user_id=str(author.get("member_openid") or author.get("user_openid")
                        or author.get("id") or ""),
            user_name=str(author.get("username") or ""),
            group_id=group_id,
            text=str(d.get("content") or "").strip(),
            message_id=str(d.get("id") or ""),
            reply=quote,
            image_urls=images,
            is_at_bot=event.get("t") == "GROUP_AT_MESSAGE_CREATE",
            user_role=str(author.get("member_role") or ""),
        )


# ══════════════════════════════════════════════════════════
#  access_token
# ══════════════════════════════════════════════════════════

class _TokenKeeper:
    """AppID + AppSecret → access_token，自动续期。

    有效期约 2 小时。提前 5 分钟续，避免正好卡在边界上。
    加锁是因为 WS 线程和 Flask 线程都会发消息。
    """

    REFRESH_MARGIN = 300

    def __init__(self, app_id: str, secret: str) -> None:
        self._app_id = app_id
        self._secret = secret
        self._token = ""
        self._expire_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str:
        with self._lock:
            if self._token and time.time() < self._expire_at - self.REFRESH_MARGIN:
                return self._token
            return self._refresh_locked()

    def _refresh_locked(self) -> str:
        try:
            r = requests.post(TOKEN_URL,
                              json={"appId": self._app_id, "clientSecret": self._secret},
                              timeout=20)
            d = r.json()
        except Exception:
            logger.exception("access_token 获取失败")
            return self._token

        token = d.get("access_token")
        if not token:
            logger.error("access_token 获取被拒: %s", str(d)[:200])
            return self._token

        self._token = token
        try:
            self._expire_at = time.time() + int(d.get("expires_in") or 7200)
        except (TypeError, ValueError):
            self._expire_at = time.time() + 7200
        logger.info("access_token 已刷新，%d 秒后过期", int(self._expire_at - time.time()))
        return self._token


# ══════════════════════════════════════════════════════════
#  客户端
# ══════════════════════════════════════════════════════════

class QQOfficialClient:
    """官方平台客户端。业务代码调用的 11 个方法都在这里。"""

    def __init__(self, app_id: str, secret: str) -> None:
        self.app_id = app_id
        self._tokens = _TokenKeeper(app_id, secret)
        self._recorder: Callable[..., None] | None = None
        self._session = requests.Session()
        self._recent_sent: list[dict[str, Any]] = []
        self._sent_lock = threading.Lock()
        # 同一条消息的被动回复序号，msg_id + msg_seq 组合唯一
        self._reply_seq: dict[str, int] = {}

    # ── 认证 ───────────────────────────────────────────────
    @property
    def access_token(self) -> str:
        """给 WebSocket 网关用。自动处理续期。"""
        return self._tokens.get()

    # ── HTTP ───────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"QQBot {self._tokens.get()}",
                "X-Union-Appid": self.app_id,
                "Content-Type": "application/json"}

    def _request(self, method: str, path: str, **kw: Any) -> dict[str, Any]:
        url = f"{API_BASE}{path}"
        try:
            r = self._session.request(method, url, headers=self._headers(),
                                      timeout=20, **kw)
        except Exception:
            logger.exception("官方接口请求失败 %s %s", method, path)
            return {}
        if r.status_code >= 400:
            logger.warning("官方接口 %s %s -> HTTP %d %s",
                           method, path, r.status_code, r.text[:200])
            return {}
        if not r.content:
            return {}
        try:
            return r.json() or {}
        except ValueError:
            return {}

    # ── 发送 ───────────────────────────────────────────────
    def _next_seq(self, msg_id: str) -> int:
        with self._sent_lock:
            seq = self._reply_seq.get(msg_id, 0) + 1
            self._reply_seq[msg_id] = seq
            # 别让它无限增长
            if len(self._reply_seq) > 500:
                self._reply_seq.clear()
                self._reply_seq[msg_id] = seq
            return min(seq, 5)      # 官方上限：每条消息最多回 5 次

    def _send(self, path: str, segments: Any, reply_to: str = "",
              group_id: str = "", target_user_id: str = "") -> bool:
        """所有发送的唯一出口：段列表 → 官方消息体。

        官方只接受纯文本 content 或富媒体 media，没有「段」的概念，
        所以 @ 被丢弃、图片走上传、文本拼接。
        """
        if isinstance(segments, str):
            segments = [{"type": "text", "data": {"text": segments}}]
        segments = segments or []

        text = _plain_text(segments)
        image_path = _first_image(segments)

        payload: dict[str, Any] = {"msg_type": MSG_TYPE_TEXT, "content": text}
        if reply_to:
            payload["msg_id"] = reply_to
            payload["msg_seq"] = self._next_seq(reply_to)

        if image_path:
            media = self._upload_image(image_path, group_id, target_user_id)
            if media:
                payload["msg_type"] = MSG_TYPE_MEDIA
                payload["media"] = media
                payload["content"] = text or " "   # media 消息也要求 content 非空
            else:
                logger.info("图片上传失败，退化成纯文本发送")

        if not payload.get("content") and payload["msg_type"] == MSG_TYPE_TEXT:
            return False

        d = self._request("POST", path, json=payload)
        ok = bool(d.get("id"))
        if ok:
            self._remember(group_id, d["id"], text, target_user_id)
        return ok

    def send_group_msg(self, group_id: str, message: Any) -> bool:
        return self._send(f"/v2/groups/{group_id}/messages", message,
                          group_id=group_id)

    def send_private_msg(self, user_id: str, message: Any) -> bool:
        return self._send(f"/v2/users/{user_id}/messages", message,
                          target_user_id=user_id)

    def reply_to(self, msg: IncomingMessage, text: str) -> bool:
        path = (f"/v2/groups/{msg.group_id}/messages" if msg.msg_type == "group"
                else f"/v2/users/{msg.user_id}/messages")
        return self._send(path, [{"type": "text", "data": {"text": text}}],
                          reply_to=msg.message_id,
                          group_id=msg.group_id, target_user_id=msg.user_id)

    def reply_image(self, msg: IncomingMessage, path: str) -> bool:
        api = (f"/v2/groups/{msg.group_id}/messages" if msg.msg_type == "group"
               else f"/v2/users/{msg.user_id}/messages")
        return self._send(api, [{"type": "image", "data": {"file": path}}],
                          reply_to=msg.message_id,
                          group_id=msg.group_id, target_user_id=msg.user_id)

    def send_text(self, msg: IncomingMessage, text: str) -> bool:
        return (self.send_group_msg(msg.group_id, MessageBuilder().text(text))
                if msg.msg_type == "group"
                else self.send_private_msg(msg.user_id, MessageBuilder().text(text)))

    # ── 富媒体 ─────────────────────────────────────────────
    def _upload_image(self, path: str, group_id: str,
                      target_user_id: str) -> dict[str, Any] | None:
        """本地图片 → 官方 file_info。

        官方发送图片必须先上传。这里用 base64（`file_data`）而不是公网 URL，
        因为我们的图都是本地文件（表情包、塔罗牌）。超过上限就放弃 ——
        退化成纯文本好过整条消息发不出去。
        """
        if not path or not os.path.exists(path):
            logger.info("图片不存在，跳过上传：%s", path)
            return None
        try:
            size = os.path.getsize(path)
            if size > MAX_INLINE_IMAGE:
                logger.warning("图片 %d 字节超过内联上限，跳过：%s", size, path)
                return None
            with open(path, "rb") as fh:
                data = base64.b64encode(fh.read()).decode()
        except OSError:
            logger.exception("读取图片失败：%s", path)
            return None

        api = (f"/v2/groups/{group_id}/files" if group_id
               else f"/v2/users/{target_user_id}/files")
        d = self._request("POST", api, json={
            "file_type": 1,                 # 1 = 图片
            "file_data": data,
            "srv_send_msg": False,
        })
        file_info = d.get("file_info") or d.get("file_uuid")
        if not file_info:
            logger.warning("富媒体上传未返回 file_info: %s", str(d)[:160])
            return None
        return {"file_info": file_info}

    # ── 撤回 ───────────────────────────────────────────────
    def recall(self, message_id: Any, group_id: str = "",
               user_id: str = "") -> bool:
        """撤回自己发的消息。官方接口按会话分路径。"""
        if not message_id:
            return False
        if group_id:
            path = f"/v2/groups/{group_id}/messages/{message_id}"
        elif user_id:
            path = f"/v2/users/{user_id}/messages/{message_id}"
        else:
            logger.info("撤回缺少会话信息，跳过")
            return False
        return bool(self._request("DELETE", path))

    # ── 会话信息 ───────────────────────────────────────────
    def get_group_info(self, group_id: str) -> dict[str, Any] | None:
        d = self._request("GET", f"/v2/groups/{group_id}/info")
        if not d:
            return None
        return {"group_id": group_id,
                "group_name": str(d.get("group_name") or d.get("group_name_short") or "")}

    def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        """官方**不提供**群成员列表接口，恒返回空。

        原来这个方法是给面板「播种群成员名单」用的；官方拿不到，
        所以成员只能在他们发言时逐个认识（每条群消息都带 username）。
        """
        logger.debug("官方平台没有群成员列表接口，返回空")
        return []

    # ── 自己发过的消息 ─────────────────────────────────────
    def set_recorder(self, recorder: Callable[..., None] | None) -> None:
        self._recorder = recorder

    def _remember(self, group_id: str, message_id: str, text: str,
                  target_user_id: str = "") -> None:
        with self._sent_lock:
            self._recent_sent.append({"message_id": message_id, "text": text,
                                      "group_id": group_id, "ts": time.time()})
            if len(self._recent_sent) > 200:
                del self._recent_sent[:-200]
        if self._recorder and group_id:
            try:
                self._recorder(group_id, message_id, text, target_user_id)
            except Exception:
                logger.debug("recorder 回调失败", exc_info=True)

    def is_own_message(self, message_id: Any = None, text: str | None = None) -> bool:
        needle = " ".join((text or "").split())
        with self._sent_lock:
            for item in self._recent_sent:
                if message_id and str(item["message_id"]) == str(message_id):
                    return True
                sent = " ".join(str(item["text"]).split())
                if len(needle) >= 6 and sent == needle:
                    return True
        return False

    # ── 群管理 ─────────────────────────────────────────────
    def leave_group(self, group_id: str) -> bool:
        """退出群聊（面板的「删除群聊」用）。"""
        return bool(self._request("DELETE", f"/v2/groups/{group_id}"))

    # ── 与原有的兼容垫片 ───────────────────────────────────
    @staticmethod
    def guess_mime(path: str) -> str:
        return mimetypes.guess_type(path)[0] or "application/octet-stream"
