# Phase 0 实测结论（QQ 官方机器人平台）

> 数据来源：AppID 102818934（机器人 `Kiriko-测试中`），
> 用 `tools/qq_probe.py` 通过 WebSocket 接入实测。
> 本文件只记录**实测到的**，不写推测；未验证的一律标注「待验证」。

## 已确定

| # | 结论 | 证据 |
|---|---|---|
| ① | **凭据与链路可用** | `access_token` 成功签发（`expires_in=7200`） |
| ② | **WebSocket 接入可用** | 收到 `READY`，带 `session_id` 与 `shard` |
| ③ | **发送路径可用** | 被动回复 `HTTP 200`，返回了 message id |
| ④ | **接入方式确实可以只用 WebSocket** | 无需回调地址、无需公网 HTTPS |

`READY` 事件里的机器人自身：

```json
{"version": 1,
 "session_id": "50144186-12fa-4f38-a371-703bbdfcd47a",
 "user": {"id": "17427809079063831668", "username": "Kiriko-测试中",
          "bot": true, "status": 1},
 "shard": [0, 1]}
```

## 关键发现一：ID 全是字符串，且没有真实 QQ 号

单聊事件 `C2C_MESSAGE_CREATE` 的 `author`：

```json
{"id":                 "466A7D064E5495F91DE04FA987EACBA3",
 "user_openid":        "466A7D064E5495F91DE04FA987EACBA3",
 "union_openid":       "466A7D064E5495F91DE04FA987EACBA3"}
```

三点要注意：

1. **`union_user_account` 字段根本没出现**（官方文档标它「可能为空」，
   实测在单聊里是直接缺席）。理论上最可能对应 QQ 号的就是这个字段。
2. **`union_openid` 与 `user_openid` 完全相同** —— 说明在这个部署里它不是
   真正的「跨应用」ID，只是同一个 openid 换个名字。
3. **`username` 也没出现** —— 只有 openid。**这意味着可能拿不到用户昵称**，
   面板上会显示成一串大写十六进制。（待群聊事件确认，见下）

### 对数据库迁移的影响

**结论：用户维度的数据基本无法映射回 QQ 号。**

现有库里 `group_messages` / `history` / `user_profiles` / `user_affection` /
`learning_log` / `bot_messages` 全部以**真实 QQ 号**为键，而官方只给 openid，
两者之间没有任何可计算的对应关系。

所以迁移只能是：

- ✅ **参考数据直接搬**：`tarot_content`、`amp_heads`、`app_versions`、
  `changelog`、`app_state`、贴图索引
- ❌ **用户数据搬不了**：画像、好感度、聊天历史、学习记录、逐条发言
  —— 只能从零重新积累
- ⚠️ **群设置手动重配**：群号 → `group_openid`

## 关键发现二：消息 id 是长字符串，不是整数

被动回复要拿原消息的 `id` 当 `msg_id`，而它是这种格式：

```
ROBOT1.0_LpwIaw7ngPGxMaqVcn7UcUA4ynteUqeAgkgSfRrwtccWWeLEkWaXOSc.WvNfdNgd1wiKZLPRSDuSd87dpUU-qLK7vOIcYRDE9IfsMXwqNnE!
```

约 120 字符、含 `.` 与 `!`。**现有库里几处 `message_id` 是 `INTEGER`**，
放不下，Phase 1 必须改成 `TEXT`：

- `bot_messages.message_id INTEGER`
- `group_messages.message_id`（需确认类型）

连带的逻辑也要改：

- `llbot_client._recent_sent` / `is_own_message` 目前按**整数**比较，
  且 `_MIN_TEXT_MATCH` 那套按文本兜底的判断也依赖 int 语义
- `_remember_sent` 现在会跳过「falsy id」（那是为 `send_group_ai_record`
  返回 0 加的）—— 字符串 id 不受影响，但判断要重新过一遍

## 关键发现三：被动回复是唯一的发送方式

- 主动推送已于 **2025-04-21** 由官方下线
- 被动回复必须带原消息 `id` 作为 `msg_id`，官方文档写的有效期是 **5 分钟**、
  每条消息最多回 **5 次**（`msg_id` + `msg_seq` 组合唯一）

实测立即回复是成功的（`HTTP 200`）。**超时后的行为待验证** ——
故意等 6 分钟再回一次即可确认。

## 待验证（下一步要测的）

| 项 | 为什么重要 |
|---|---|
| **群聊事件**（`GROUP_AT_MESSAGE_CREATE`） | 单聊测不出群相关的一切；`author` 在群里可能多带 `member_openid` / `union_user_account` / `username` |
| **全量模式**（`GROUP_MESSAGE_CREATE`，非 @ 的群消息） | **决定群语境 / 活跃统计 / 聊天回看能不能保留**；文档说需要开「接收所有消息」开关，但是否要审批未知 |
| **群成员能否拿到昵称** | 拿不到的话面板要自己维护「openid → 显示名」映射 |
| **被动回复超时（>5 分钟）的实际报错** | 决定回复策略 |
| **消息长度上限** | 决定要不要切分 |

## 对计划的影响（待群聊结果后定稿）

- Phase 4「数据库迁移」的用户数据那一层要**删掉**，只保留参考数据
- Phase 1 要加一项：`message_id` 列 `INTEGER → TEXT`（涉及迁移脚本）
- 面板要加「openid → 显示名」的映射层
- 「箱头推荐改成按需查询工具」这个决定更重要了 ——
  定时推送没了，箱头库只能靠用户主动问
