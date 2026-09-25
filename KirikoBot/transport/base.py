"""传输层接口：把「机器人核心」和「用哪个平台」解耦。

## 为什么需要它

现在整个项目直接依赖 `llbot_client.LLBotClient`（OneBot 11 / NapCat）。
要接 QQ 官方平台，最省事的做法是到处 `if 平台 ==`，那会烂得很快。
所以中间夹一层：**核心逻辑只认这里的接口，平台差异全部关在适配器里**。

## 接口是从真实调用点反推的，不是设计出来的

全项目对 `llbot` 的调用只有 12 个方法，分布在 5 个文件：

```
main.py             send_group_msg send_private_msg get_group_info
                    get_group_member_list set_recorder
robot_server.py     reply_to reply_image send_text
ai_tools.py         send_group_msg send_private_msg send_ai_voice
                    get_ai_characters recall is_own_message
scheduler.py        send_group_msg
version_manager.py  send_group_msg
```

**刻意保持这么小**：接口越大，两个平台的适配成本越高，能同时满足的
交集也越小。

## 能力差异用 `Capabilities` 表达，而不是到处判断平台

两个平台能做的事并不一样（官方没有主动推送、没有 AI 语音、不能 @ 群友）。
与其在业务代码里到处写 `if transport.name == "qqofficial"`，不如让适配器
**声明自己的能力**，业务代码查这个表来决定某个功能要不要出现。

这同时就是迁移计划里「移除不支持的功能」那一步的落点 ——
功能开关（`feature_gate`）可以读 `Capabilities`，而不是硬编码删代码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """一个传输层能做什么。业务代码据此决定功能是否出现。

    每一项都对应项目里真实存在的功能，不是设想出来的：
    """

    #: 主动发消息（不经由用户消息触发）。官方平台 2025-04-21 起已下线，
    #: 因此定时推送（早报/一言/发言榜/箱头推荐）和提醒都依赖它。
    proactive: bool = True

    #: 收到群里**没有 @ 机器人**的消息。群语境、活跃统计、聊天回看依赖它。
    group_context: bool = True

    #: 发 AI 语音（OneBot 走 NapCat 扩展接口；官方平台没有）。
    voice: bool = True

    #: 在消息里 @ 某个群成员。
    mention_member: bool = True

    #: 撤回自己发过的消息。
    recall: bool = True

    #: 引用某条消息来回复（官方平台的 message_reference 标着【暂未支持】，
    #: 但能**收到**引用信息）。
    quote_reply: bool = True

    #: 消息里可以用本地文件路径直接发图（官方平台要先上传拿 file_info）。
    local_image_path: bool = True

    #: 被动回复的时间窗口（秒）。官方平台是 5 分钟，OneBot 没有限制。
    #: `None` 表示不受限。
    reply_window_seconds: int | None = None


@runtime_checkable
class Transport(Protocol):
    """机器人核心用到的全部平台能力。

    实现类必须同时提供 `name` 和 `capabilities`，业务代码只读这两个，
    不去判断具体是哪个平台。
    """

    name: str
    capabilities: Capabilities

    # ── 发送 ────────────────────────────────────────────────
    def send_group_msg(self, group_id: str, message: Any) -> bool:
        """往群里发一条消息。`message` 是 MessageBuilder 构建的消息段列表。"""

    def send_private_msg(self, user_id: str, message: Any) -> bool:
        """私聊发一条消息。"""

    def reply_to(self, msg: Any, text: str) -> bool:
        """回复某条消息。群里通常带 @ 对方；私聊直接回。"""

    def reply_image(self, msg: Any, path: str) -> bool:
        """用图片回复某条消息。"""

    def send_text(self, msg: Any, text: str) -> bool:
        """在同一个会话里发纯文本，但不引用原消息。"""

    # ── 会话信息 ────────────────────────────────────────────
    def get_group_info(self, group_id: str) -> dict[str, Any] | None:
        """群的基本信息（至少要有 group_name）。取不到返回 None。"""

    def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        """群成员列表，元素至少含 user_id / user_name。取不到返回空列表。"""

    # ── 消息状态 ────────────────────────────────────────────
    def recall(self, message_id: Any) -> bool:
        """撤回一条自己发的消息。"""

    def is_own_message(self, message_id: Any = None, text: str | None = None) -> bool:
        """判断一条被引用的消息是不是自己发的。"""

    def set_recorder(self, recorder: Callable[..., None] | None) -> None:
        """注册「自己发出的消息」的回调，用于撤回和引用识别。

        回调签名：`recorder(group_id, message_id, text, target_user_id)`
        """

    # ── 可选能力：能力表里为 False 时，业务代码不应调用 ──────
    def send_ai_voice(self, group_id: str, character: str, text: str) -> bool:
        """发 AI 语音。`capabilities.voice` 为 False 时返回 False。"""

    def get_ai_characters(self) -> list[dict[str, Any]]:
        """可用音色列表。不支持时返回空列表。"""


@dataclass
class TransportInfo:
    """给面板/日志用的一点点自描述，避免把整个适配器暴露出去。"""

    name: str
    capabilities: Capabilities
    detail: dict[str, Any] = field(default_factory=dict)
