"""OneBot 11 / NapCat 适配器：把现有的 `LLBotClient` 包成 `Transport`。

这是**过渡层**，不是重写。`llbot_client.py` 里的逻辑一行不动，
这里只做两件它没有的事：

1. **声明能力表** —— OneBot/NapCat 什么都能做（主动推送、看全部群消息、
   AI 语音、@ 群友、撤回），所以 `Capabilities` 全是 True。
   接官方平台时那张表会大量变 False，业务代码据此决定功能是否出现。
2. **提供一个稳定的名字**，让业务代码和面板不必知道底层是哪个客户端。

等官方平台那条路走通、OneBot 分支真的退役时，这个文件连同
`llbot_client.py` 一起删掉即可 —— 核心代码不会受影响。
"""

from __future__ import annotations

from typing import Any, Callable

from llbot_client import LLBotClient

from .base import Capabilities


class OneBotTransport:
    """`Transport` 的 OneBot 实现。纯委托，不含业务判断。"""

    name = "onebot"

    #: OneBot/NapCat 全都支持，所以这里全是 True。
    #: 注意 `reply_window_seconds=None` —— 被动回复没有 5 分钟那种限制，
    #: 定时推送也不受窗口约束。
    capabilities = Capabilities()

    def __init__(self, client: LLBotClient) -> None:
        self._client = client

    # ── 发送 ────────────────────────────────────────────────
    def send_group_msg(self, group_id: str, message: Any) -> bool:
        return self._client.send_group_msg(group_id, message)

    def send_private_msg(self, user_id: str, message: Any) -> bool:
        return self._client.send_private_msg(user_id, message)

    def reply_to(self, msg: Any, text: str) -> bool:
        return self._client.reply_to(msg, text)

    def reply_image(self, msg: Any, path: str) -> bool:
        return self._client.reply_image(msg, path)

    def send_text(self, msg: Any, text: str) -> bool:
        return self._client.send_text(msg, text)

    # ── 会话信息 ────────────────────────────────────────────
    def get_group_info(self, group_id: str) -> dict[str, Any] | None:
        return self._client.get_group_info(group_id)

    def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        return self._client.get_group_member_list(group_id)

    # ── 消息状态 ────────────────────────────────────────────
    def recall(self, message_id: Any) -> bool:
        return self._client.recall(message_id)

    def is_own_message(self, message_id: Any = None, text: str | None = None) -> bool:
        return self._client.is_own_message(message_id, text)

    def set_recorder(self, recorder: Callable[..., None] | None) -> None:
        self._client.set_recorder(recorder)

    # ── 可选能力 ────────────────────────────────────────────
    def send_ai_voice(self, group_id: str, character: str, text: str) -> bool:
        return self._client.send_ai_voice(group_id, character, text)

    def get_ai_characters(self) -> list[dict[str, Any]]:
        return self._client.get_ai_characters()
