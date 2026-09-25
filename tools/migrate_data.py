#!/usr/bin/env python3
"""把旧部署（OneBot / NapCat）里的**参考数据**迁到 QQ 官方平台部署。

## 为什么只能迁一部分

官方平台换了一套 ID：群和用户不再是 QQ 号，而是 `group_openid` / `user_openid`
（同一个 AppID 下一人一号，换 AppID 就全变）。旧库里所有以 QQ 号为主键的数据
——聊天记录、画像、好感度、学习日志、塔罗历史、工具调用日志——在新部署里
**对不上任何人**，迁过去只会变成一堆没人能触达的孤儿行，所以一律不迁。

能迁的是「和具体是谁无关」的参考数据：

| 表 | 是什么 | 为什么值得迁 |
|---|---|---|
| `tarot_content` | 44 张塔罗牌的牌义文案 | 纯文案，和用户无关 |
| `amp_heads` | 77 条吉他箱头资料 | 纯资料，爬虫已删，这是唯一来源 |
| `app_versions` | 版本号记录 | 面板「版本日志」页的骨架 |
| `changelog` | 93 条版本日志正文 | 历史记录，删了就没了 |
| `stickers` | 表情包索引（1404 条） | 贴图本体已经复制到本地，索引必须跟上 |

`ai_calls`（AI 用量）默认不迁：它带 `group_id`，而旧群号在新部署里显示不出名字，
会在用量页上多出一批幽灵群。确实想要历史曲线的话加 `--include-usage`。

## 用法

    # 先看要做什么，不写任何东西
    python3 tools/migrate_data.py --source /path/to/old/robot.db --dry-run

    # 真迁（目标库不存在时才会建；已存在则要求 --force）
    python3 tools/migrate_data.py --source /path/to/old/robot.db

    # 连同 648 张只在旧工作目录里、没进 git 的贴图一起复制过来
    python3 tools/migrate_data.py --source /path/to/old/robot.db --copy-stickers

脚本是幂等的：`--force` 会先清空目标库里的这几张参考表再重灌，
不会重复插入，也不会碰任何用户数据表。
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
KIRIKO_DIR = REPO_ROOT / "KirikoBot"
DEFAULT_TARGET = KIRIKO_DIR / "robot.db"
DEFAULT_STICKER_DIR = KIRIKO_DIR / "stickers"

# (表名, 说明)。顺序有意义：app_versions 必须先于 changelog 落地，
# 否则 changelog.version_id 会指向还不存在的版本行。
REFERENCE_TABLES: list[tuple[str, str]] = [
    ("app_versions", "版本号记录"),
    ("changelog", "版本日志正文"),
    ("tarot_content", "塔罗牌义文案"),
    ("amp_heads", "吉他箱头资料库"),
    ("stickers", "表情包索引（只迁本地有文件的那些）"),
]

# 明确不迁的表 + 原因。打印给使用者看，免得以为是漏了。
SKIPPED_TABLES: list[tuple[str, str]] = [
    ("history", "按 QQ 号存，官方平台是 openid，对不上"),
    ("group_messages", "同上（且不再接收非 @ 群消息）"),
    ("bot_messages", "同上"),
    ("user_profiles", "同上"),
    ("profile_history", "同上"),
    ("user_affection", "同上"),
    ("user_affection_log", "同上"),
    ("user_mood", "同上"),
    ("learning_log", "同上"),
    ("tarot_history", "同上（每日一抽的限额记录）"),
    ("tool_usage", "同上"),
    ("feature_requests", "同上"),
    ("group_subscriptions", "定时推送功能已删除"),
    ("reminders", "提醒功能已删除（依赖主动推送）"),
    ("feature_settings", "键是旧群号，对不上新群"),
    ("app_state", "只存爬虫状态，爬虫已删除"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _local_sticker_files(sticker_dir: Path) -> set[str]:
    if not sticker_dir.is_dir():
        return set()
    return {p.name for p in sticker_dir.iterdir() if p.is_file()}


def copy_stickers(source_db: Path, sticker_dir: Path, dry_run: bool) -> tuple[int, int]:
    """把旧库旁边的贴图文件补齐到本地目录。

    只补缺的，不覆盖已存在的（已存在的可能已经被重新分过类/裁过）。
    返回 (复制数, 源里总共多少)。
    """
    src_dir = source_db.parent / "stickers"
    if not src_dir.is_dir():
        print(f"  ! 在 {src_dir} 没找到贴图目录，跳过")
        return 0, 0
    names = sorted(p.name for p in src_dir.iterdir() if p.is_file())
    have = _local_sticker_files(sticker_dir)
    todo = [n for n in names if n not in have]
    if not dry_run and todo:
        sticker_dir.mkdir(parents=True, exist_ok=True)
        for n in todo:
            shutil.copy2(src_dir / n, sticker_dir / n)
    return len(todo), len(names)


def migrate(
    source_db: Path,
    target_db: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    include_usage: bool = False,
    copy_sticker_files: bool = False,
) -> int:
    if not source_db.is_file():
        print(f"源库不存在：{source_db}", file=sys.stderr)
        return 2
    if source_db.resolve() == target_db.resolve():
        print("源库和目标库是同一个文件，拒绝执行。", file=sys.stderr)
        return 2
    if target_db.exists() and not force and not dry_run:
        print(
            f"目标库已存在：{target_db}\n"
            "如果确实要往里灌参考数据（会先清空这几张表），加 --force。",
            file=sys.stderr,
        )
        return 2

    tables = list(REFERENCE_TABLES)
    if include_usage:
        tables.append(("ai_calls", "AI 用量历史（带旧群号，只为留住曲线）"))

    # ---- 目标库：用项目自己的 DatabaseManager 建表，保证 schema 和代码一致 ----
    if not dry_run:
        sys.path.insert(0, str(KIRIKO_DIR))
        from database_manager import DatabaseManager  # noqa: PLC0415

        DatabaseManager(str(target_db))

    src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    dst = None if dry_run else sqlite3.connect(str(target_db))

    sticker_dir = DEFAULT_STICKER_DIR if copy_sticker_files else None
    if copy_sticker_files:
        moved, total = copy_stickers(source_db, sticker_dir, dry_run)
        verb = "待复制" if dry_run else "已复制"
        print(f"贴图文件：源目录 {total} 个，{verb} {moved} 个 -> {sticker_dir}")

    local_files = _local_sticker_files(DEFAULT_STICKER_DIR)
    print(f"本地贴图文件：{len(local_files)} 个")

    print()
    print(f"{'表':22} {'源':>7} {'迁入':>7}  说明")
    print("-" * 72)

    failures: list[str] = []
    for table, note in tables:
        if not _table_exists(src, table):
            print(f"{table:22} {'-':>7} {'-':>7}  源库没有这张表，跳过")
            continue
        src_count = src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]

        if dry_run:
            print(f"{table:22} {src_count:>7} {'?':>7}  {note}")
            continue

        if not _table_exists(dst, table):
            print(f"{table:22} {src_count:>7} {'0':>7}  ! 目标库没有这张表（功能被删了？）")
            failures.append(table)
            continue

        src_cols = _table_columns(src, table)
        dst_cols = _table_columns(dst, table)
        cols = [c for c in src_cols if c in dst_cols]
        if not cols:
            print(f"{table:22} {src_count:>7} {'0':>7}  ! 两边没有同名列")
            failures.append(table)
            continue

        dst.execute(f'DELETE FROM "{table}"')
        collist = ", ".join(f'"{c}"' for c in cols)
        rows = src.execute(f'SELECT {collist} FROM "{table}"').fetchall()

        # 贴图索引要过滤：只保留本地真的有文件的条目，否则表情工具会
        # 返回一个指向不存在文件的路径，发出去直接失败。
        if table == "stickers":
            name_idx = cols.index("filename")
            kept = [r for r in rows if r[name_idx] in local_files]
            skipped = len(rows) - len(kept)
            rows = kept
            if skipped:
                print(f"{table:22} {src_count:>7} {len(rows):>7}  {note}"
                      f"（丢掉 {skipped} 条本地没有文件的）")
            else:
                print(f"{table:22} {src_count:>7} {len(rows):>7}  {note}")
        else:
            print(f"{table:22} {src_count:>7} {len(rows):>7}  {note}")

        placeholders = ", ".join("?" for _ in cols)
        dst.executemany(
            f'INSERT INTO "{table}" ({collist}) VALUES ({placeholders})', rows
        )
        dst.commit()

    src.close()
    if dst is not None:
        dst.close()

    print()
    print("以下表有意不迁（ID 空间不同 / 功能已删）：")
    for table, why in SKIPPED_TABLES:
        print(f"  - {table:22} {why}")

    if failures:
        print()
        print("有表没能迁进去：" + ", ".join(failures), file=sys.stderr)
        return 1
    if dry_run:
        print()
        print("（--dry-run：什么都没写）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把旧 OneBot 部署的参考数据迁到 QQ 官方平台部署",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--source", required=True, type=Path,
                    help="旧库路径，例如 <旧仓库>/KirikoBot/robot.db")
    ap.add_argument("--target", type=Path, default=DEFAULT_TARGET,
                    help=f"目标库路径（默认 {DEFAULT_TARGET}）")
    ap.add_argument("--force", action="store_true",
                    help="目标库已存在时也继续（会先清空要迁的那几张表）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只报告会迁什么，不写任何东西")
    ap.add_argument("--include-usage", action="store_true",
                    help="连 ai_calls 一起迁（注意：带旧群号，用量页会多出幽灵群）")
    ap.add_argument("--copy-stickers", action="store_true",
                    help="把旧库旁边 stickers/ 里本地缺的文件也复制过来")
    args = ap.parse_args()
    return migrate(
        args.source,
        args.target,
        force=args.force,
        dry_run=args.dry_run,
        include_usage=args.include_usage,
        copy_sticker_files=args.copy_stickers,
    )


if __name__ == "__main__":
    raise SystemExit(main())
