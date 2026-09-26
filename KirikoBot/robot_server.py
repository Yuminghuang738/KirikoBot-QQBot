from __future__ import annotations

import logging
from typing import Any

from qq_official import IncomingMessage, MessageBuilder, QQOfficialClient

logger = logging.getLogger(__name__)


class RobotServer:
    """Thin wrapper: parses incoming messages and delegates sending to QQOfficialClient."""

    def __init__(self, msg_data: dict[str, Any], client: QQOfficialClient, bot_qq: str) -> None:
        self.client = client
        self.incoming = IncomingMessage.from_event(msg_data, bot_qq)
        self.text: str = ""
        self.image_path: str = ""

        logger.info(
            "Message: type=%s user=%s(%s) group=%s",
            self.incoming.msg_type, self.incoming.user_name,
            self.incoming.user_id, self.incoming.group_id,
        )

    # Proxy common attributes from IncomingMessage
    @property
    def msg_type(self) -> str:
        return self.incoming.msg_type

    @property
    def user_id(self) -> str:
        return self.incoming.user_id

    @property
    def group_id(self) -> str | None:
        return self.incoming.group_id

    @property
    def group_name(self) -> str | None:
        return self.incoming.group_name

    @property
    def user_name(self) -> str:
        return self.incoming.user_name

    @property
    def user_role(self) -> str | None:
        return self.incoming.user_role

    @property
    def user_level(self) -> str | None:
        return self.incoming.user_level

    @property
    def user_title(self) -> str | None:
        return self.incoming.user_title

    @property
    def msg(self) -> str:
        return self.incoming.text

    @property
    def at_judgement(self) -> bool:
        return self.incoming.is_at_bot

    # ── Sending ──────────────────────────────────────────

    def send(self, message: Any) -> bool:
        """发送任意段列表（文本/图片/混合）作为**被动回复**。

        工具要自己发消息时用这个，**不要**去碰 `client` 上按 id 发送的方法 ——
        官方平台没有主动推送，不带原消息 `msg_id` 的发送一律 400
        （`40034105 主动消息失败, 无权限`）。这里自动把收到的那条消息带上。
        """
        return self.client.send(self.incoming, message)

    def reply(self, text: str) -> bool:
        """Reply to incoming message. Groups: reply+@user+text. Private: reply+text."""
        return self.client.reply_to(self.incoming, text)

    def send_text(self, text: str) -> bool:
        """Send plain text without @ prefix."""
        return self.client.send_text(self.incoming, text)

    def reply_image(self, path: str) -> bool:
        """Send image as reply."""
        return self.client.reply_image(self.incoming, path)

