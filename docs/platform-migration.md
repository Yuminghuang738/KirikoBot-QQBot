# 从 OneBot 迁移到 QQ 官方平台：做了什么、为什么、怎么回退

这个仓库是上游 [KirikoBot（LLBot / OneBot 版）](../../KirikoBot) 的分线。
本文记的是**这次迁移本身的工程决策**，不是平台 API 的实测数据
（后者在 [`phase0-findings.md`](phase0-findings.md)）。

---

## 1. 为什么要重写接入层，而不是加一层适配

最初试过写一个传输层抽象，让 OneBot 和官方平台两套实现共存在同一份代码里。
**放弃了这个方案**，原因是两者的能力集合不是「交集 + 各自特例」，而是**官方平台严格更小**：

- 主动推送（`send_group_msg` 到任意群）—— 官方平台没有
- 非 @ 群消息 —— 官方平台默认收不到
- 语音合成、@群成员 —— 官方平台没有

也就是说适配层要为 100% 的调用点准备两条路径，而其中一条永远是「不支持」。
更糟的是，那些不支持的能力在 OneBot 侧是**很多功能的根基**（群语境、群统计、提醒、定时播报），
留着一个「能编译但永远失败」的分支，只会让后来的人以为它能用。

最后选择**直接面向官方平台重写接入层**（`qq_official.py` + `qq_gateway.py`），
把用不上的功能整块删掉。删掉的清单和理由见 README 的
「与 OneBot 版的差异」一节。

### 被删掉的传输层（可作参考）

`transport/` 目录被创建后又被删除。它是一个 `Transport` 协议 + 两个实现
（OneBot / QQ 官方）的选择器。删除它的理由是：抽象层的价值来自**有多个真实实现**，
而这里第二个实现注定是残缺的，抽象只增加了间接层，没有增加能力。

---

## 2. ID 空间：为什么用户数据一律不迁

| | OneBot | 官方平台 |
|---|---|---|
| 群 | QQ 群号（`193392307`） | `group_openid` |
| 用户 | QQ 号 | `user_openid`（**按 AppID 分发**） |
| 机器人自己 | QQ 号 | 只有 AppID |

`user_openid` 是**每个 AppID 一套**的。实测同一个用户在同一 AppID 下，
事件里的 `id`、`member_openid`、`union_openid` 是同一个值；
`union_user_account` 字段不存在。换 AppID，这串值全变。

所以旧库里的所有 `user_id` / `group_id`（QQ 号）在新部署里**不可能**映射到任何人。
迁移脚本 `tools/migrate_data.py` 因此只搬参考数据，把哪些表不迁、为什么，
逐条打印出来给使用者看 —— 免得以为脚本漏了。

> 顺带一个坑：官方消息 id 是 ~120 字符的字符串（形如 `ROBOT1.0_...`，含 `.` 和 `!`），
> 不是数字。旧库把 `message_id` 声明成 `INTEGER`，读取路径里还有 `int(message_id)`
> 强转 —— 遇到官方 id 会直接 `ValueError`。迁移时列类型改成 `TEXT`，强转去掉。

---

## 3. 容器与部署的变化

旧的 compose 有三个服务：`pmhq`（跑 QQ 客户端）、`llbot`（OneBot 实现 + WebUI）、`my-robot`。
官方平台**不需要本地登录一个 QQ 客户端**，所以 `pmhq` 和 `llbot` 整个消失，
`qq_volume`（QQ 登录态设备卷）也不需要了 —— 扫码、设备锁、风控那一整套随之消失。
整栈只剩 `my-robot` 一个服务。

容器名从 `kirikobot` 改成 `kirikobot-qq`：两个 compose 项目同时存在时，
同名容器会被对方 `up` 的时候抢走或重建。

`.env` 里 `ONEBOT_API` / `ONEBOT_TOKEN` / `WEBHOOK_TOKEN` 被 `QQ_APP_ID` / `QQ_APP_SECRET` 取代；
入站 webhook 没有了（改成 WebSocket 网关长连接），所以 `webhook_auth.py` 也删了。

---

## 4. 如果哪天「全量模式」能开了，怎么捡回来

官方平台的 `GROUP_MESSAGE_CREATE`（不带 @ 的普通群消息）理论上可用，
条件是**群管理员在机器人资料页里开启通知**。本部署实测找不到这个入口，因此按不可用处理。

如果将来能开，下面这些是可以恢复的（代码在 git 历史里，本分支删除前都在）：

| 功能 | 恢复要点 |
|---|---|
| 群语境感知 | 恢复 `ReadContextTool` + `prompt_builder.format_group_context`；`GROUP_MESSAGE_CREATE` 事件要接进 `qq_gateway.py` 的 `MESSAGE_EVENTS` |
| 群消息存档 / 聊天回看 | `group_messages` 表还在（只是不再写入非 @ 消息），恢复写入路径 + `get_group_message_page` |
| 群活跃统计 / 发言榜 | 依赖上面的存档 |
| 表情包被动收集 | `sticker_collector` 的收集入口 + 视觉分类 |

**注意**：这些恢复都需要先确认 `GROUP_MESSAGE_CREATE` 真的推得过来、且频率/配额可接受 ——
它会把**群里每一条消息**都推到机器人，量级和现在完全不是一个数量级。

---

## 5. 不可能恢复的

以下三条是平台能力缺失，不是配置问题，删掉的代码不要再加回来：

- **主动推送**（2025-04-21 官方下线）→ 定时播报、提醒、版本发布通知永久不可用
- **AI 语音合成** → 没有对应接口
- **@群成员** → 平台不允许

机器人现在是「**只有被 @ 时才存在**」的形态。

---

## 6. 引用消息的负载结构（实测 + 官方文档对照）

这一段是补写的：迁移时**没有**记录过引用消息长什么样，而代码里写的形状是错的，
结果引用感知静默失效了很久（不报错，只是永远拿不到被引内容）。

### 实测抓到的普通消息（`events.jsonl`，未经引用）

```json
{
  "id": "ROBOT1.0_Ja5gygCo...",
  "content": " 你好",
  "message_type": 0,
  "group_openid": "A968B3FFD260C6D9FF38FA671BC543F5",
  "message_scene": {
    "source": "default",
    "ext": [
      "msg_idx=REFIDX_yTz+NP4EOZBSKsITE9PrjA==",
      "auth_token=CJc-kAGN47_mRoKFDL3WLg"
    ]
  }
}
```

**注意 `msg_idx` 每条消息都有** —— 它是「本条消息自己的索引」，不是引用标记。
拿它的存在判断引用会把所有消息都当成引用。

### 官方文档定义的引用消息

依据 [群@机器人消息](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_at_message_create.html)：

| 位置 | 字段 | 含义 |
|---|---|---|
| `d` | `message_type` | `0`=普通文本，`3`=结构化卡片，`101`=并行消息，`102`=聊天记录，**`103`=引用消息** |
| `d.msg_elements[]` | `content` / `author.username` | 被引用的正文 / 被引用的作者 |
| `message_scene.ext` | `msg_idx` | 本条消息自己的索引（恒有） |
| `message_scene.ext` | **`ref_msg_idx`** | **被引用**消息的索引（只在引用场景出现） |

所以「这条消息是不是引用」要看 `message_type == 103` **或** `ref_msg_idx` 存在 ——
两个信号都认。

### 一个反直觉的地方：索引和消息 id 不是一套编号

文档示例里 `ref_msg_idx=TMP_1111-2222`，而消息 id 是 `ROBOT1.0_...`。
**两者不能直接互换**，所以「拿被引索引去 `bot_messages.message_id` 里查」多半查不到。

真正可靠的做法是：事件本来就给了**被引正文**，而机器人自己的发言正文我们是有记录的
（`bot_messages.text` + `target_user_id`）。用正文精确匹配就能同时得到
「这是我说的」和「我当初说给谁的」—— 后者正是「用户 B 引用了机器人说给 A 的话，
机器人要意识到换了个人」这条功能的核心。见 `database_manager._find_quoted_by_text`。

正文兜底沿用 `QQOfficialClient.is_own_message` 的 6 字符护栏，
并且跳过 `recalled = 1` 的行（撤回过的发言不该再被引用）。

### 踩过的三个坑（都在同一条链路上）

1. **只认元素级的 `message_type == 103`**：103 是**消息级**字段，`msg_elements`
   里的元素通常不带它 → 引用压根解不出来。
2. **读 `reply.message_seq`**：`QuoteInfo` 上只有 `message_id`，没有 `message_seq`
   → `AttributeError` 被 `except` 吞掉，反查自己记录那一步从未执行。
   现在由 `prompt_builder.quote_ref_id` 兼容两代字段名（官方字段优先）。
3. **`dataclasses.replace(reply, target_name=...)`**：`QuoteInfo` 没有
   `target_name` 字段 → 走到最后一步直接 `TypeError`。该字段现在补上了
   （它由我们自己的库反查填充，不在官方事件里）。

三个坑的共同点：**都表现为「功能不生效」而不是「报错」**（第 3 个是唯一会抛的，
但它被前两个挡在后面）。这类问题只能靠「端到端地把一个真实形状的事件走完整条链路」
的测试发现 —— 零件测试一个都不缺，照样全绿。
