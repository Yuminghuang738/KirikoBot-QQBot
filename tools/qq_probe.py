#!/usr/bin/env python3
"""Phase 0 probe for the QQ official bot platform (开放平台 / api.sgroup.qq.com).

This exists to answer three questions **before** any of the real refactor
starts, because the answers decide whether the migration is worth doing at all:

1. **Can we authenticate and reach the OpenAPI?**  (AppID + AppSecret ->
   access_token -> /users/@me). Trivial, but it proves the credentials and the
   network path.

2. **Does an event carry anything that maps back to a real QQ number?**
   The official platform hands out `openid`s, not QQ numbers, and every table
   in the existing database is keyed on QQ numbers. The event schema mentions
   `union_openid` / `union_user_account`, both documented as "可能为空". If
   `union_user_account` is the QQ number, the existing profiles / affection /
   history can be migrated by mapping on the fly. If it is empty, user data
   can only be re-accumulated. **This single answer is the biggest fork in the
   whole plan**, which is why it gets its own probe.

3. **Which group events actually arrive, and does 全量模式 need approval?**
   `GROUP_AT_MESSAGE_CREATE` is the documented @-only event;
   `GROUP_MESSAGE_CREATE` ("接收所有消息") is what the group-context, activity
   stats and transcript features depend on. The docs say it needs a toggle, but
   not whether that toggle is gated. Also worth measuring: the passive-reply
   window (5 minutes / max 5 replies per message) and whether long messages are
   rejected.

It deliberately does **not** import anything from the project — the whole point
is to learn the shape of the platform before writing a line of adapter code.

Usage
-----
    QQ_APP_ID=102818934 QQ_APP_SECRET=... python3 tools/qq_probe.py
    QQ_APP_ID=... QQ_APP_SECRET=... python3 tools/qq_probe.py --watch 600
    QQ_APP_ID=... QQ_APP_SECRET=... python3 tools/qq_probe.py --dump events.jsonl

While it is watching, go and talk in the group (both plain messages and ones
that @ the bot), then come back and read the summary.

Nothing is sent to the group. The probe is read-only by design, so it cannot
trip QQ's spam controls on the account you are trying to keep clean.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import requests

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
API_BASE = "https://api.sgroup.qq.com"

# Intent for QQ group + single-chat events. The docs list
# GROUP_AND_C2C_EVENT as (1 << 25); it covers both
# GROUP_AT_MESSAGE_CREATE and GROUP_MESSAGE_CREATE.
INTENT_GROUP_AND_C2C = 1 << 25

# Websocket opcodes.
OP_DISPATCH, OP_HEARTBEAT, OP_IDENTIFY, OP_RESUME = 0, 1, 2, 6
OP_RECONNECT, OP_INVALID_SESSION, OP_HELLO, OP_HEARTBEAT_ACK = 7, 9, 10, 11

# Fields we specifically want to inspect in message events. `union_*` is the
# one that decides whether the old database can be carried over.
MAPPING_FIELDS = ("id", "user_openid", "member_openid", "union_openid",
                  "union_user_account", "username")


def log(msg: str = "") -> None:
    print(msg, flush=True)


def get_access_token(app_id: str, secret: str) -> str:
    """AppID + AppSecret -> access_token (valid ~2 hours)."""
    log("[1] 获取 access_token")
    r = requests.post(TOKEN_URL, json={"appId": app_id, "clientSecret": secret},
                      timeout=20)
    log(f"    HTTP {r.status_code}")
    try:
        d = r.json()
    except ValueError:
        log(f"    非 JSON 响应: {r.text[:200]}")
        sys.exit(1)
    if not d.get("access_token"):
        log(f"    失败: {json.dumps(d, ensure_ascii=False)[:300]}")
        log("    （AppSecret 不对，或者这个机器人还没通过审核）")
        sys.exit(1)
    log(f"    OK  expires_in={d.get('expires_in')} 秒  "
        f"token={d['access_token'][:12]}…")
    return d["access_token"]


def api_get(token: str, path: str) -> tuple[int, Any]:
    r = requests.get(f"{API_BASE}{path}",
                     headers={"Authorization": f"QQBot {token}",
                              "X-Union-Appid": os.environ.get("QQ_APP_ID", "")},
                     timeout=20)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def probe_rest(token: str) -> str:
    """Prove the token works and report the bot's own identity."""
    log("[2] GET /users/@me")
    code, d = api_get(token, "/users/@me")
    log(f"    HTTP {code}  {json.dumps(d, ensure_ascii=False)[:300]}")

    log("[3] GET /gateway")
    code, d = api_get(token, "/gateway")
    log(f"    HTTP {code}  {json.dumps(d, ensure_ascii=False)[:300]}")
    if code != 200 or not isinstance(d, dict) or not d.get("url"):
        log("    拿不到 gateway，后面的 WebSocket 走不了")
        sys.exit(1)
    return d["url"]


async def watch(gateway: str, token: str, seconds: int, dump: str | None) -> None:
    """Connect, identify, and print every event verbatim."""
    try:
        import websockets  # noqa: PLC0415  (optional dependency, probe only)
    except ImportError:
        log("    缺少 websockets 库：pip install websockets")
        sys.exit(1)

    log(f"[4] WebSocket  {gateway}")
    seq: int | None = None
    events: list[dict[str, Any]] = []
    session_id = ""
    deadline = time.time() + seconds
    dump_fh = open(dump, "a", encoding="utf-8") if dump else None

    try:
        async with websockets.connect(gateway, max_size=2 ** 24) as ws:
            hello = json.loads(await ws.recv())
            interval = (hello.get("d") or {}).get("heartbeat_interval", 30000) / 1000
            log(f"    HELLO  heartbeat_interval={interval}s")
            log(f"    IDENTIFY  intents={INTENT_GROUP_AND_C2C} (1<<25)")

            await ws.send(json.dumps({
                "op": OP_IDENTIFY,
                "d": {
                    "token": f"QQBot {token}",
                    "intents": INTENT_GROUP_AND_C2C,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "kirikobot-probe",
                                   "$device": "kirikobot-probe"},
                },
            }))

            async def heartbeat() -> None:
                while True:
                    await asyncio.sleep(interval)
                    await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": seq}))

            hb = asyncio.create_task(heartbeat())
            log(f"\n    开始监听 {seconds} 秒 —— 现在去群里说两句"
                "（一条普通消息、一条 @机器人），然后回来看结果\n")

            try:
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except asyncio.TimeoutError:
                        continue
                    pkt = json.loads(raw)
                    op = pkt.get("op")
                    if pkt.get("s") is not None:
                        seq = pkt["s"]
                    if op == OP_HELLO:
                        continue
                    if op == OP_HEARTBEAT_ACK:
                        continue
                    if op in (OP_RECONNECT, OP_INVALID_SESSION):
                        log(f"    op={op}（{pkt.get('d')}）—— 需要重连，先退出")
                        break
                    if op != OP_DISPATCH:
                        log(f"    op={op} {json.dumps(pkt, ensure_ascii=False)[:160]}")
                        continue

                    session_id = session_id or str(pkt.get("d", {}).get("session_id", ""))
                    events.append(pkt)
                    if dump_fh:
                        dump_fh.write(json.dumps(pkt, ensure_ascii=False) + "\n")
                        dump_fh.flush()
                    print_event(pkt)
            finally:
                hb.cancel()
    finally:
        if dump_fh:
            dump_fh.close()

    summarise(events)


def print_event(pkt: dict[str, Any]) -> None:
    t = pkt.get("t", "?")
    d = pkt.get("d") or {}
    log("─" * 70)
    log(f"事件 t={t}")
    if not t.endswith("MESSAGE_CREATE"):
        log(f"  {json.dumps(d, ensure_ascii=False)[:300]}")
        return

    author = d.get("author") or {}
    log(f"  group_openid = {d.get('group_openid') or '(单聊)'}")
    log(f"  content      = {str(d.get('content'))[:80]!r}")
    ids = {k: author.get(k) for k in MAPPING_FIELDS if author.get(k)}
    log(f"  author       = {json.dumps(ids, ensure_ascii=False)}")
    if not ids.get("union_openid") and not ids.get("union_user_account"):
        log("    ⚠ 没有任何 union_* 字段 —— 这个用户映射不回 QQ 号")
    if d.get("attachments"):
        log(f"  attachments  = "
            f"{[a.get('content_type') for a in d['attachments']]}")
    if d.get("msg_elements"):
        log(f"  msg_elements = "
            f"{[e.get('message_type') for e in d['msg_elements']]}")
    ext = ((d.get("message_scene") or {}).get("ext")) or []
    if ext:
        log(f"  scene.ext    = {ext}")


def summarise(events: list[dict[str, Any]]) -> None:
    """Answer the three questions in plain language."""
    log("\n" + "=" * 70)
    log("结论")
    log("=" * 70)

    kinds: dict[str, int] = {}
    saw_union = saw_union_value = False
    union_examples: list[str] = []
    for pkt in events:
        kinds[pkt.get("t", "?")] = kinds.get(pkt.get("t", "?"), 0) + 1
        a = (pkt.get("d") or {}).get("author") or {}
        if a.get("union_openid") or a.get("union_user_account"):
            saw_union = True
            for k in ("union_openid", "union_user_account"):
                if a.get(k):
                    saw_union_value = True
                    union_examples.append(f"{k}={a[k]}")

    log(f"收到的事件类型: {kinds or '（一个都没收到）'}")
    log("")
    if not events:
        log("① 凭据与网络：无法判断（没收到事件，先确认机器人已进群、"
            "且已开启相应事件订阅）")
    else:
        log("① 凭据与网络：OK，能收到事件")
    log("")
    if kinds.get("GROUP_MESSAGE_CREATE"):
        log("② 全量群消息（GROUP_MESSAGE_CREATE）：✅ 可用 —— "
            "群语境 / 活跃统计 / 聊天回看 都能保留")
    elif kinds.get("GROUP_AT_MESSAGE_CREATE"):
        log("② 全量群消息（GROUP_MESSAGE_CREATE）：❌ 没收到，只收到 @ 事件 —— "
            "要么「接收所有消息」没开，要么需要额外申请")
    else:
        log("② 全量群消息：无法判断（群里发过普通消息吗？）")
    log("")
    if saw_union_value:
        log("③ 能否映射回 QQ 号：✅ 事件里有 union_* 且有值 —— "
            "现有用户数据有希望迁移")
        for ex in union_examples[:3]:
            log(f"     {ex}")
    elif saw_union:
        log("③ 能否映射回 QQ 号：⚠ 字段存在但为空 —— 文档说的「可能为空」"
            "真的会发生，需要另想办法")
    else:
        log("③ 能否映射回 QQ 号：❌ 事件里完全没有 union_* 字段 —— "
            "用户历史只能重新积累，数据库迁移只剩下参考数据那一层")
    log("")
    log("（把上面这段连同事件的原始 JSON 一起发我，我来定后面的方案）")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", type=int, default=120,
                    help="监听多少秒（默认 120）")
    ap.add_argument("--dump", default=None, help="把原始事件追加写到这个文件")
    args = ap.parse_args()

    app_id = os.environ.get("QQ_APP_ID") or ""
    secret = os.environ.get("QQ_APP_SECRET") or ""
    if not app_id or not secret:
        log("需要环境变量 QQ_APP_ID 和 QQ_APP_SECRET")
        log("  QQ_APP_ID=102818934 QQ_APP_SECRET=xxx python3 tools/qq_probe.py")
        sys.exit(2)

    token = get_access_token(app_id, secret)
    gateway = probe_rest(token)

    log("\n提示：如果下面一个事件都收不到，先确认")
    log("  · 机器人已经加入群（或用了「内部体验号码」）")
    log("  · 开放平台后台「事件订阅」里已勾选群相关事件")
    log("  · 群里发过一条普通消息和一条 @机器人 的消息")
    try:
        asyncio.run(watch(gateway, token, args.watch, args.dump))
    except KeyboardInterrupt:
        log("\n（手动中断）")


if __name__ == "__main__":
    main()
