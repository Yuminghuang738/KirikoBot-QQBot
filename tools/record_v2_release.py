#!/usr/bin/env python3
"""把 v2.0.0（迁移到 QQ 官方平台）的版本记录与变更日志写进数据库。

## 为什么单独一个脚本

`tools/migrate_data.py` 搬的是**旧部署的**参考数据（版本历史到 1.20.0 为止）。
而 2.0.0 是这次迁移本身产生的，旧库里当然没有，所以要在迁移**之后**单独记一笔。

顺序不能反：`migrate_data.py --force` 会清空 `app_versions` / `changelog` 再重灌，
先记 2.0.0 再迁移的话会被覆盖掉。所以部署配方是：

    python3 tools/migrate_data.py --source <旧库> --copy-stickers
    python3 tools/record_v2_release.py

脚本是**幂等**的：2.0.0 已经存在就直接退出，不会写重复记录。
它只写数据库和 `KirikoBot/VERSION`，不碰任何其他表。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIRIKO_DIR = REPO_ROOT / "KirikoBot"
VERSION = "2.0.0"

RELEASE_SUMMARY = (
    "整体迁移到 QQ 官方机器人平台：不再依赖 LLBot / NapCat 和本地登录的 QQ 客户端，"
    "改为直接对接官方开放平台。代价是官方平台从原理上拿不到的一批能力被删除。"
)

# (entry_type, 标题, 正文)
ENTRIES: list[tuple[str, str, str]] = [
    ("breaking", "整体迁移到 QQ 官方机器人平台",
     "接入方式换了一整层：凭据从 ONEBOT_API/ONEBOT_TOKEN 变成 QQ_APP_ID/QQ_APP_SECRET，"
     "事件从 LLBot 用 HTTP 推到我们的 /webhook 变成主动连官方 WebSocket 网关长连接，"
     "发消息必须带一条 5 分钟内的原消息 msg_id（被动回复），每条消息最多回 5 次。"
     "整个 pmhq + llbot 两个容器和 QQ 登录态设备卷一起删掉了 —— 不需要在本地跑一个"
     "登录着的 QQ 客户端，扫码、设备锁、风控那一整套随之消失，整栈只剩一个服务。"
     "群和用户的标识也从 QQ 号换成了 openid（同一个 AppID 下一人一号，换 AppID 全变），"
     "实测事件的 id / member_openid / union_openid 恒等，且没有 union_user_account —— "
     "没有任何能对应回 QQ 号的东西。"),

    ("breaking", "删掉官方平台从原理上做不到的功能",
     "定时推送（早安新闻 / 游戏速递 / 每日一言 / 发言榜 / 版本发布通知）和提醒：官方平台"
     "2025-04-21 起没有主动推送，这两类只能被动回复的功能整体不可实现。群语境感知、"
     "群活跃统计、发言榜、聊天回看、群消息存档、表情包被动收集：默认收不到没 @ 机器人的"
     "群消息（全量模式需要群管理员在机器人资料页开通知，本部署实测找不到这个入口）。"
     "AI 语音：官方没有语音合成接口。@群友：平台不允许。LLBot WebUI 整合（连接状态原生页 + "
     "WebQQ iframe）：不再有 LLBot 这个东西。用户画像 / 好感度 / 自学习保留了，但能观察到的"
     "样本从「整群发言」缩到「被 @ 的对话」，判断会明显更粗。表情包库现在是静态的，不再增长。"),

    ("breaking", "用户历史数据迁不过去，只迁了参考数据",
     "openid 和 QQ 号是两套完全不同的 ID 空间，旧库里的聊天记录、画像、好感度、学习日志、"
     "塔罗历史、工具调用日志在新部署里对不上任何人，迁过去只会变成没人能触达的孤儿行，"
     "所以一律不迁。新增 tools/migrate_data.py，只搬和具体是谁无关的参考数据：塔罗牌义（44 条）、"
     "箱头资料（77 条）、版本号与版本日志（31 + 93 条）、表情包索引（1404 条，只迁本地真有文件的）、"
     "需求清单（8 条 —— 那是项目自己的待办历史，不是用户数据，昵称是反规范化存的，显示不依赖 openid）。"
     "脚本会把「哪些表不迁、为什么」逐条打印出来，免得下次看的人以为是漏了；--force 幂等重灌，"
     "不碰任何其他表。"),

    ("fix", "引用感知曾经静默失效：消息 id 不是数字",
     "官方消息 id 是约 120 字符的字符串（形如 ROBOT1.0_xxx.yyy!zzz，含 . 和 !），"
     "而旧库把 message_id 声明成 INTEGER，读取路径里还有 int(message_id) 强转 —— "
     "遇到官方 id 直接 ValueError，然后被 except (TypeError, ValueError) 吞成「没找到」。"
     "症状不是报错，而是引用了消息机器人却完全不知道，属于最难查的那种。"
     "现在两张表的 message_id 都改成 TEXT（SQLite 不能改列类型，所以对旧库做了 PRAGMA 探测 + "
     "同事务整表重建，顺带处理了「重命名会把旧索引一起带走、导致后面 CREATE INDEX IF NOT EXISTS "
     "变成静默 no-op」这个坑），强转去掉，老库里存的整数 id 原样保留、查询依然命中。"),

    ("fix", "「删除群聊并让机器人退群」只删数据、不退群",
     "面板的删除群聊调的是 OneBot 时代的 client.call(\"set_group_leave\", ...)，而官方客户端"
     "根本没有 call()（只有 leave_group）。AttributeError 被 except Exception 吞掉，"
     "于是数据 purge 掉了、机器人还在群里，界面上还容易以为退成功了。改用官方的 leave_group()。"
     "这也是全项目最后一处 client.call。"),

    ("improve", "箱头推荐从每日推送改成按需工具",
     "原来它只有「每天定时推一条到群里」这一条路径，随主动推送一起删掉后，七十多条从 Wikipedia "
     "整理的资料就只剩面板「箱头库」页能翻。现在群友问「推荐个箱头」就会给一条。复用原来的"
     "按日轮转（不是 ORDER BY RANDOM() —— 随机会连着两天抽到同一个，看着像 bug），所以同一天"
     "所有人拿到的是同一个箱头，方便群里聊起来。空字段不硬写，价格明确标为参考值。"),

    ("feature", "面板新增「连接状态」页",
     "原来的「LLBot 连接」组整个删掉，换成一个「连接」组。新页面读 /status，显示 AppID、"
     "网关是否连上、最近一次事件/心跳时间、运行时长和调度器状态。/status 为此补了 app_id 和 "
     "gateway 两个字段。后端没给的字段一律显示「—」，不猜。"),
]


def main() -> int:
    sys.path.insert(0, str(KIRIKO_DIR))
    from database_manager import DatabaseManager  # noqa: PLC0415
    from version_manager import VersionManager  # noqa: PLC0415

    db_file = KIRIKO_DIR / "robot.db"
    if not db_file.is_file():
        print(f"没找到 {db_file}。先跑 tools/migrate_data.py 把库建起来。", file=sys.stderr)
        return 2

    db = DatabaseManager(str(db_file))
    vm = VersionManager(db)

    existing = vm.get_current_version()
    if existing and existing.get("version") == VERSION:
        print(f"数据库里已经有 {VERSION} 了（id={existing.get('id')}），什么都不做。")
        return 0
    if existing:
        print(f"当前库里最新版本是 {existing.get('version')}，"
              f"比要写的 {VERSION} 旧 —— 说明迁移还没做，现在补记 {VERSION}。")

    # VersionManager 按**当前工作目录**写 VERSION 文件，所以这里显式切到 KirikoBot/，
    # 否则会把版本号写到仓库根目录那个没人读的 VERSION 上（上游遗留）。
    import os  # noqa: PLC0415
    os.chdir(KIRIKO_DIR)

    ver = vm.create_version(VERSION, RELEASE_SUMMARY, author="dashboard")
    print(f"已创建版本 {ver['version']} (id={ver['id']})")
    for entry_type, title, desc in ENTRIES:
        vm.add_changelog(ver["id"], entry_type, title, desc, author="dashboard")
        print(f"  + [{entry_type}] {title}")
    print(f"VERSION 文件现在: {vm.read_version_file()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
