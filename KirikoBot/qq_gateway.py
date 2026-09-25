"""QQ 官方平台的 WebSocket 网关：收事件 → 派发给业务逻辑。

**这是替换 `/webhook` 的东西，方向反过来了。**

原架构是 LLBot 用 HTTP 把事件推到我们的 Flask 路由（被动接收）。
官方平台走 WebSocket —— 得**我们自己维持一条长连接**去收。
所以 Flask 里那个 `/webhook` 路由作废，改成一个后台线程。

选 WebSocket 而不是 Webhook 是因为：Webhook 需要一个**公网 HTTPS 地址**
（域名 + 证书 + 端口转发），而这个部署在一台家用 NAT 后面的机器上。
WebSocket 是出站连接，什么都不用配。

## 事件类型

- `GROUP_AT_MESSAGE_CREATE` —— 群里 @ 机器人（主力）
- `C2C_MESSAGE_CREATE` —— 单聊
- `GROUP_MESSAGE_CREATE` —— 群里**所有**消息（全量模式）。需要群管理员在
  QQ 里开启，本部署的群里没有这个入口，所以实际上收不到。
  代码照样处理它 —— 万一哪天开了，不用改代码就能用。
- 其余（`READY` / `GROUP_ADD_ROBOT` / 心跳应答等）只记日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

GATEWAY_URL = "https://api.sgroup.qq.com/gateway"

# QQ 群 + 单聊事件。官方文档：GROUP_AND_C2C_EVENT = 1 << 25。
# GROUP_AT_MESSAGE_CREATE 和 GROUP_MESSAGE_CREATE 用同一个 intent。
INTENT_GROUP_AND_C2C = 1 << 25

# 真正要交给业务处理的事件。全量模式那个也留着 —— 见模块说明。
MESSAGE_EVENTS = frozenset({
    "GROUP_AT_MESSAGE_CREATE",
    "GROUP_MESSAGE_CREATE",
    "C2C_MESSAGE_CREATE",
})

OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY = 0, 1, 2
OP_RECONNECT, OP_INVALID_SESSION, OP_HELLO, OP_HEARTBEAT_ACK = 7, 9, 10, 11

# 重连退避：连不上时别把日志刷爆，也别永远等下去。
RECONNECT_MIN, RECONNECT_MAX = 3, 120


class GatewayClient:
    """后台线程里跑的 WebSocket 客户端。

    `on_event` 收到的是**原始事件字典**，由调用方决定怎么变成 RobotServer ——
    网关只管收和重连，不碰业务，所以它不需要知道任何项目内部结构。
    """

    def __init__(self, token_provider: Callable[[], str],
                 on_event: Callable[[dict[str, Any]], None],
                 gateway_url: str = GATEWAY_URL) -> None:
        self._token = token_provider
        self._on_event = on_event
        self._gateway_url = gateway_url
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._seq: int | None = None
        self._session_id = ""
        self._connected = False
        self._last_event_at = 0.0

    # ── 生命周期 ───────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="qq-gateway")
        self._thread.start()
        logger.info("QQ 网关线程已启动")

    def stop(self) -> None:
        self._stop.set()

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def status(self) -> dict[str, Any]:
        """给面板/健康检查看的一点点状态。"""
        return {"connected": self._connected,
                "last_event_at": self._last_event_at,
                "session_id": self._session_id}

    # ── 主循环 ─────────────────────────────────────────────
    def _run(self) -> None:
        backoff = RECONNECT_MIN
        while not self._stop.is_set():
            try:
                asyncio.run(self._session())
                backoff = RECONNECT_MIN          # 正常结束，重置退避
            except Exception:
                logger.exception("QQ 网关连接异常")
            self._connected = False
            if self._stop.is_set():
                break
            logger.info("%d 秒后重连 QQ 网关", backoff)
            if self._stop.wait(backoff):
                break
            backoff = min(backoff * 2, RECONNECT_MAX)

    async def _session(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("缺少 websockets 依赖，网关无法启动（pip install websockets）")
            self._stop.set()
            return

        url = self._gateway_url
        try:
            import requests
            r = requests.get(url, headers={"Authorization": f"QQBot {self._token()}",
                                           "X-Union-Appid": ""},
                             timeout=20)
            if r.status_code == 200:
                url = (r.json() or {}).get("url") or url
        except Exception:
            logger.debug("取 gateway 地址失败，用默认值", exc_info=True)

        async with websockets.connect(url, max_size=2 ** 24) as ws:
            hello = json.loads(await ws.recv())
            interval = (hello.get("d") or {}).get("heartbeat_interval", 30000) / 1000
            await ws.send(json.dumps({
                "op": OP_IDENTIFY,
                "d": {"token": f"QQBot {self._token()}",
                      "intents": INTENT_GROUP_AND_C2C,
                      "shard": [0, 1],
                      "properties": {"$os": "linux", "$browser": "kirikobot",
                                     "$device": "kirikobot"}},
            }))
            self._connected = True
            logger.info("QQ 网关已连接，心跳间隔 %.1fs", interval)

            hb = asyncio.create_task(self._heartbeat(ws, interval))
            try:
                while not self._stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    self._handle(ws, raw)
            finally:
                hb.cancel()

    async def _heartbeat(self, ws: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))

    def _handle(self, ws: Any, raw: Any) -> None:
        try:
            pkt = json.loads(raw)
        except (ValueError, TypeError):
            return

        op = pkt.get("op")
        if pkt.get("s") is not None:
            self._seq = pkt["s"]

        if op == OP_HELLO:
            return
        if op == OP_HEARTBEAT_ACK:
            return
        if op in (OP_RECONNECT, OP_INVALID_SESSION):
            logger.warning("网关要求重连 (op=%s, %s)", op, pkt.get("d"))
            raise ConnectionError("gateway asked to reconnect")
        if op != OP_DISPATCH:
            return

        event_type = str(pkt.get("t") or "")
        self._last_event_at = time.time()
        d = pkt.get("d") or {}
        self._session_id = str(d.get("session_id") or self._session_id)

        if event_type not in MESSAGE_EVENTS:
            # READY / GROUP_ADD_ROBOT / 成员变动等 —— 记一行就够。
            logger.info("网关事件 %s %s", event_type,
                        json.dumps(d, ensure_ascii=False)[:160])
            return

        try:
            self._on_event(pkt)
        except Exception:
            # 单条事件处理失败绝不能把网关连接带下去。
            logger.exception("事件处理失败: %s", event_type)
