"""传输层：让机器人核心不依赖具体的聊天平台。

    from transport import Capabilities, Transport
    from transport.onebot import OneBotTransport

    if transport.capabilities.proactive:
        ...  # 只有支持主动推送的平台才做定时推送

目前只有 OneBot 一个实现；QQ 官方平台的适配器（`transport/qqofficial.py`）
是 Phase 2 的活。
"""

from .base import Capabilities, Transport, TransportInfo

__all__ = ["Capabilities", "Transport", "TransportInfo"]
