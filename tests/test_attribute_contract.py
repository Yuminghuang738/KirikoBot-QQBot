"""静态契约检查：不要访问不存在的属性。

## 为什么需要这个

迁移到官方平台后，「旧字段名残留」这一类 bug 咬过 **三次**，而且每次症状都
不一样、都不容易联想到原因：

1. `reply.message_seq` —— `QuoteInfo` 上只有 `message_id`。`AttributeError`
   被宽泛的 `except` 吞掉，于是「引用的是机器人说给别人的话」永远识别不出来
   （静默失效，不报错）。
2. `dataclasses.replace(reply, target_name=...)` —— `QuoteInfo` 少了这个字段，
   走到最后一步直接 `TypeError`。
3. `robot.incoming.message_seq` / `reply.message_seq` 出现在
   `db.record_group_message(...)` 里 —— **每条群消息**都在这里崩，
   用户看到的是「抱歉，处理消息时遇到了问题，请稍后再试~」。私聊不走那一行，
   所以症状只在群里出现。

第 3 个尤其说明问题：它就在我眼皮底下，前面几轮 grep 都没抓到 —— 因为我的
过滤条件里带了 `grep -v "message_seq,"`，把那一行自己滤掉了。
**靠人眼和临时的 grep 抓不住这类问题，得有会一直跑下去的检查。**

## 检查方式

从源码解析出三个类真正拥有的属性名（dataclass 注解字段、`@property`、
方法、以及 `__init__` 里赋的实例属性），然后扫描业务代码里对这些对象的
属性访问，发现名字不在集合里就失败。
"""
from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "KirikoBot"))

APP_DIR = Path(__file__).resolve().parent.parent / "KirikoBot"

# 扫描范围。不含 database_manager.py —— 那里的 `robot` 是另一个东西
# （表名参数），不是 RobotServer。
SCANNED_FILES = ["main.py", "ai_tools.py", "prompt_builder.py",
                 "robot_server.py", "chat_history.py"]


def _class_attrs(source: str, class_name: str) -> set[str]:
    """一个类真正拥有的属性：注解字段 + 方法/property + self.X 赋值。

    注意 `self.text: str = ""` 这种「带注解的实例属性」是 `AnnAssign` 且目标为
    `Attribute`，和 `self.text = ""`（`Assign`）不是同一个节点 —— 只收后者会
    把存在的属性误判成不存在（这个检查器第一版就踩了）。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        allowed: set[str] = set()
        # 类体直接子节点：dataclass 注解字段、方法名
        for sub in node.body:
            if isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                allowed.add(sub.target.id)
            elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                allowed.add(sub.name)
        # 任意位置的 self.X = ... / self.X: T = ...
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                allowed |= _self_targets(sub.targets)
            elif isinstance(sub, ast.AnnAssign):
                allowed |= _self_targets([sub.target])
        return allowed
    raise AssertionError(f"没找到类 {class_name}")


def _self_targets(targets) -> set[str]:
    out: set[str] = set()
    for t in targets:
        if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                and t.value.id == "self"):
            out.add(t.attr)
    return out


def _allowed() -> dict[str, set[str]]:
    qq = (APP_DIR / "qq_official.py").read_text(encoding="utf-8")
    rs = (APP_DIR / "robot_server.py").read_text(encoding="utf-8")
    return {
        "IncomingMessage": _class_attrs(qq, "IncomingMessage"),
        "QuoteInfo": _class_attrs(qq, "QuoteInfo"),
        "RobotServer": _class_attrs(rs, "RobotServer"),
        "QQOfficialClient": _class_attrs(qq, "QQOfficialClient"),
        "MessageBuilder": _class_attrs(qq, "MessageBuilder"),
    }


def test_the_check_itself_works():
    """先证明这套解析真的能拿到属性 —— 否则后面全是假绿。"""
    allowed = _allowed()
    assert {"message_id", "text", "user_id", "group_id"} <= allowed["IncomingMessage"]
    assert {"message_id", "text", "sender_name", "target_name"} <= allowed["QuoteInfo"]
    # RobotServer 在 __init__ 里赋的实例属性也要被解析到
    assert {"client", "incoming", "text", "image_path"} <= allowed["RobotServer"]
    # 代理属性
    assert {"msg_type", "user_id", "group_id"} <= allowed["RobotServer"]


def _scan(patterns: list[tuple[re.Pattern[str], str]], allowed: dict[str, set[str]]):
    """返回 [(对象, 属性, 文件:行)]"""
    problems: list[tuple[str, str, str]] = []
    for name in SCANNED_FILES:
        path = APP_DIR / name
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for pattern, obj in patterns:
                for match in pattern.finditer(code):
                    attr = match.group(1)
                    if attr not in allowed[obj]:
                        problems.append((obj, attr, f"{name}:{lineno}"))
    return problems


def test_no_access_to_nonexistent_message_attributes():
    allowed = _allowed()
    problems = _scan(
        [(re.compile(r"\brobot\.incoming\.(\w+)"), "IncomingMessage"),
         # main.py 里的 `reply` 始终来自 robot.incoming.reply，就是 QuoteInfo
         (re.compile(r"(?<![\w.])reply\.(\w+)"), "QuoteInfo")],
        allowed,
    )
    # ai_tools.py 里的 `reply` 是 MessageBuilder（发送侧），不是 QuoteInfo ——
    # 只对 main.py 断言 reply.*，避免误报。
    problems = [p for p in problems
                if not (p[2].startswith("ai_tools") and p[0] == "QuoteInfo")]
    assert not problems, (
        "访问了不存在的属性（这会让整条消息处理崩掉，而且被 except 吞掉时是静默失效）：\n"
        + "\n".join(f"  {obj}.{attr}  ← {loc}" for obj, attr, loc in problems)
    )


def test_no_access_to_nonexistent_robot_attributes():
    allowed = _allowed()
    problems = _scan([(re.compile(r"\brobot\.(\w+)"), "RobotServer")], allowed)
    assert not problems, (
        "RobotServer 上没有这些属性：\n"
        + "\n".join(f"  robot.{attr}  ← {loc}" for _, attr, loc in problems)
    )


def test_incoming_message_has_no_onebot_seq_fields():
    """把「它没有 message_seq」钉住 —— 谁再写就会在这里看到为什么。"""
    allowed = _allowed()
    assert "message_seq" not in allowed["IncomingMessage"]
    assert "message_seq" not in allowed["QuoteInfo"]
    # OneBot 的字段名一个都不该在
    for dead in ("post_type", "raw_message", "message_type", "sub_type", "notice_type"):
        assert dead not in allowed["IncomingMessage"]


def test_no_access_to_nonexistent_client_methods():
    """`client.X` 必须是 QQOfficialClient 上真实存在的方法/属性。

    这条是冲着**线上真实踩过的坑**来的：`client.send_group_msg(group_id, ...)`
    曾经是 OneBot 兼容垫片，它不带原消息 `msg_id`，官方平台一律拒绝：

        HTTP 400 {"message":"主动消息失败, 无权限","code":40034105}

    结果是**所有自包含工具**（塔罗 / 搜索 / 点歌 / 新闻 / 一言 / 表情包…）
    全部静默失败：工具执行了、消息被平台退掉，用户看到的是「机器人不回话」。
    现在那两个垫片已经被删掉，这个检查保证它们不会再偷偷回来。
    """
    allowed = _allowed()
    problems = _scan([(re.compile(r"\bclient\.(\w+)"), "QQOfficialClient")], allowed)
    assert not problems, (
        "QQOfficialClient 上没有这些成员：\n"
        + "\n".join(f"  client.{attr}  ← {loc}" for _, attr, loc in problems)
    )


def test_the_proactive_push_shims_stay_deleted():
    """把「不能有主动发送」这件事钉在契约层。

    官方平台只能被动回复（发消息必须带 5 分钟内的原消息 id），
    所以任何 `send_group_msg(group_id, ...)` 形状的 API 都是陷阱。
    """
    allowed = _allowed()
    for dead in ("send_group_msg", "send_private_msg"):
        assert dead not in allowed["QQOfficialClient"], (
            f"client.{dead} 又回来了 —— 官方平台没有主动推送，这条路必定 400")


def _message_builder_vars(source: str) -> set[str]:
    """文件里所有被赋值为 `MessageBuilder()` 的变量名。

    用「追踪变量」而不是「变量名以 builder 结尾」—— 后者会漏掉 `mb = MessageBuilder()`
    这种写法，护栏就变成空转的了（第一版就是这么写的，植入 bug 都测不出来）。
    """
    return set(re.findall(r"(\w+)\s*=\s*MessageBuilder\(", source))


def test_no_calls_to_nonexistent_message_builder_methods():
    """凡是 `MessageBuilder` 实例上调用的方法，都必须是真实存在的段方法。

    这条抓的是**同一次线上事故的第二个和第三个坑**：点歌工具里

        music_builder.music(music_type, str(song_id))    # OneBot 的音乐卡片段
        record_builder.record(audio_path)               # OneBot 的语音段

    官方平台的 `MessageBuilder` 只有 text / image / at / reply —— 没有 music，
    也没有 record。第一个直接在群里抛 `AttributeError`（用户看到「点歌有问题」），
    第二个排在它后面，修好第一个就会立刻撞上。
    """
    allowed = _allowed()
    builder_methods = allowed["MessageBuilder"]
    assert {"text", "image", "at", "reply", "build"} <= builder_methods

    problems: list[str] = []
    for name in SCANNED_FILES:
        src = (APP_DIR / name).read_text(encoding="utf-8")
        vars_ = _message_builder_vars(src)
        for lineno, line in enumerate(src.splitlines(), 1):
            code = line.split("#", 1)[0]
            # 直接链式：MessageBuilder().xxx(...)
            for attr in re.findall(r"MessageBuilder\(\)\.(\w+)", code):
                if attr not in builder_methods:
                    problems.append(f"MessageBuilder().{attr}  ← {name}:{lineno}")
            # 变量形式：mb.xxx(...)
            for var, attr in re.findall(r"\b(\w+)\.(\w+)", code):
                if var in vars_ and attr not in builder_methods:
                    problems.append(f"{var}.{attr}  ← {name}:{lineno}")

    assert not problems, (
        "MessageBuilder 上没有这些段方法：\n" + "\n".join(f"  {p}" for p in problems)
    )


def test_the_onebot_segment_types_stay_gone():
    """OneBot 的段类型不能在 MessageBuilder 上复活。

    官方平台没有「段」的概念，只有 text / image，其余（音乐卡片、语音、@）
    要么不存在（music/record），要么会被 `_plain_text` 丢弃（at）。
    """
    allowed = _allowed()
    for dead in ("music", "record", "video", "file", "share", "json", "face", "poke"):
        assert dead not in allowed["MessageBuilder"], (
            f"MessageBuilder.{dead} 又回来了 —— 官方平台没有这个段类型")
