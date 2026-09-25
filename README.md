# 🤖 KirikoBot — 基于 QQ 官方机器人平台 + DeepSeek 的 QQ 聊天机器人

一个接入 **QQ 官方机器人平台**（[q.qq.com](https://q.qq.com)）的 QQ 机器人，集成 **DeepSeek V4.1 Flash** 大模型，提供智能对话、工具调用、版本管理等能力。机器人采用可爱女孩风格（Kiriko），偶尔傲娇。

> **这个仓库是从 [KirikoBot（LLBot / OneBot 版）](../KirikoBot) fork 出来的分线。**
> 上游那份依赖 LLBot + NapCat 在本地跑一个登录着的 QQ 客户端；这份**直接对接 QQ 官方开放平台**，
> 不需要任何本地 QQ 客户端，也就没有扫码登录、设备锁、风控那一整套。
> 代价是官方平台有一批能力**从原理上就拿不到**，相关功能已经删除 —— 详见下方
> [与 OneBot 版的差异](#-与-onebot-版的差异已删除的功能)。

---

## ✨ 主要功能

### 💬 AI 对话
- DeepSeek V4.1 Flash（模型名 `deepseek-flash`），支持思维链
- 群里 @机器人 或私聊触发，多轮对话记忆
- 用户画像分析 + 自学习反馈
- **引用感知**：群友引用某条消息说话时，能看懂被引用的是谁、说了什么——包括识别出那是在引用它自己说过的话，以及**识别出引用的是它当初说给另一个人听的**（不会把新来的人当成原来那个人继续聊）
- **情绪阶梯**：被同一个人反复骚扰时逐级升级（正常 → 不耐烦 → 发脾气 → 摆烂不干活），有冷却期

### 🛠 内置工具
| 工具 | 说明 |
|------|------|
| 🃏 塔罗牌 | 随机抽牌 + AI 解读，**每天限一张**，另有历史查询 |
| 🎵 点歌 | 网易云搜歌 → QQ 音乐卡片播放 |
| 🎸 箱头推荐 | 从吉他箱头资料库里按需给一条（年份/功率/电子管/音色/参考价） |
| 🔍 联网搜索 | 实时搜索 + AI 总结 |
| 🌤 天气查询 | 全国城市天气 + 预报 |
| 🎲 掷骰子 | D6 / D20 / D100 |
| 🍜 吃什么 | 随机美食推荐 |
| 📰 时政新闻 | BBC / VOA 翻译播报（按需查询） |
| 🎮 游戏新闻 | 热点游戏资讯（按需查询） |
| 📺 B站热搜 | Bilibili 热门排行 |
| 💬 一言 | 随机 Hitokoto 语录 |
| 💰 余额查询 | DeepSeek API 余额 |
| 😊 表情包 | 随机发表情 / 应要求找表情 / 表情对战 / 找库里最像的一张 |
| ❤️ 好感度 | 查询当前好感度与好感榜 |
| ↩️ 撤回消息 | 撤回机器人自己刚发的消息 |
| 🪞 解释自己 | 回放上一轮调用了什么工具、当时在想什么 |
| 🕐 当前时间 | 让模型拿到准确时间 |
| 📋 功能建议 | 群友提需求 → 自动收录，也可查询清单 |

### 🖥 Web 管理面板
- 总览 / 实时日志（SSE）/ 对话记录 / 用户画像 / 好感度 / 自学习
- 塔罗 / 表情包画廊 / 箱头资料库 / 功能清单
- 版本日志 / 群管理（含整群数据删除）/ AI 用量 / 群设置 / 连接状态

### 📦 版本管理
语义化版本号 `X.Y.Z`，变更日志分类归档（🎉新功能 / 🔧修复 / 💡改进 / ⚠️重大变更），面板可浏览。
**不再自动推送到群里**——官方平台没有主动推送能力。

---

## 📋 前置要求

- 一台可运行 Docker 的机器（Linux / Windows WSL2 / macOS）
- **一个 QQ 开放平台的机器人**（个人开发者即可申请，见下方第 2 步）
- [DeepSeek API Key](https://platform.deepseek.com/api_keys)
- 不需要 QQ 账号密码，不需要扫码，不需要 LLBot / NapCat

---

## 🚀 快速部署

### 1. 克隆并准备配置

```bash
git clone https://github.com/Yuminghuang738/KirikoBot-QQBot.git
cd KirikoBot-QQBot
cp KirikoBot/.env.example KirikoBot/.env
```

### 2. 申请机器人，拿 AppID / AppSecret

到 [QQ 开放平台](https://q.qq.com) → 创建机器人（选「群聊机器人」）→
在 **开发设置** 页拿到：

- **AppID**（形如 `102xxxxxx`）
- **AppSecret**（等价于密码，别外传）

填进 `KirikoBot/.env`：

```ini
QQ_APP_ID     = "你的AppID"
QQ_APP_SECRET = "你的AppSecret"

DEEPSEEK_API   = "https://api.deepseek.com/chat/completions"
DEEPSEEK_TOKEN = "你的DeepSeek API Key"
DEEPSEEK_MODEL = "deepseek-flash"

# 机器人 QQ 号：官方平台事件里不带这个，只用于面板/日志显示，可留空
ROBOT_QQ = ""
```

> 关于**沙箱**：机器人创建后默认处于「沙箱环境」，只有开发者自己（以及你加的测试成员）
> 能把它拉进群。要让所有人可用需要提交上架审核。开发调试用沙箱就够了。

### 3. 启动

```bash
docker compose up -d
```

面板在 `http://localhost:5000`（首次启动会生成访问口令，见下方「🔐 访问口令」）。
面板的 **连接 → 连接状态** 页可以看到网关是否连上。

然后把机器人拉进一个群，@它说话即可。

> **改完 `.env` 必须重建容器**，`restart` 不会重新读取环境变量：
> ```bash
> docker compose up -d --force-recreate my-robot
> ```

---

## 🔌 官方平台的硬性限制（决定了机器人怎么用）

这几条不是 bug，是官方平台的设计，**直接影响你能期待什么**：

| 限制 | 后果 |
|---|---|
| **只能被动回复**：收到消息后 5 分钟内可以用它当 `msg_id` 回，每条消息最多回 5 次 | 没有主动推送，也就没有定时播报、定时提醒 |
| **主动推送已于 2025-04-21 下线** | 同上 |
| **默认收不到没 @ 机器人的群消息** | 看不到群里的普通闲聊，因此没有群语境/群统计/聊天回看 |
| **群和用户是 openid，不是 QQ 号** | 面板里显示的是 openid；旧数据按 QQ 号存，无法对应（见[数据迁移](#-数据迁移)） |
| **没有 AI 语音合成接口** | 语音回复做不了 |
| **不允许 @ 群成员** | @群友做不了 |

「收不到非 @ 群消息」这一条理论上有全量模式（`GROUP_MESSAGE_CREATE`），需要**群管理员在机器人资料页里开启通知**。本部署实测**找不到这个入口**，因此按没有处理。如果哪天能开，群语境/群统计那批功能可以再捡回来。

更详细的一手实测结论（token、网关、事件结构、引用段字段等）记在 [`docs/phase0-findings.md`](docs/phase0-findings.md)。

---

## 🔀 与 OneBot 版的差异（已删除的功能）

| 已删除 | 为什么 |
|---|---|
| 定时推送（早安新闻 / 游戏速递 / 每日一言 / 发言榜） | 官方平台没有主动推送 |
| 提醒（定时提醒、每日重复） | 同上 |
| 版本发布自动推送到所有群 | 同上 |
| 群语境感知（`read_context` 工具 + 背景注入） | 收不到非 @ 的群消息 |
| 群活跃统计 / 发言榜 / 聊天回看 / 群消息存档 | 同上 |
| 表情包**被动收集** | 同上（收集靠监听所有群消息） |
| AI 语音（`send_voice`） | 官方平台没有语音合成接口 |
| @群友（`at_member`） | 官方平台不允许 |
| LLBot WebUI 整合（连接状态原生页 + WebQQ iframe） | 不再有 LLBot 这个东西 |

**改造后保留**：**箱头推荐**。原来是「每天定时推一条到群里」，靠的就是主动推送；
现在改成 AI 按需调用的工具（群友问「推荐个箱头」才给），资料库、按日轮转的逻辑
和「同一天所有人拿到同一个」的性质都保留了。

**降级但保留**：用户画像 / 好感度 / 自学习。以前能观察整个群的发言，现在只能看到「被 @ 的那些对话」，
样本明显变小，判断会更粗——能力还在，精度回不到从前。

**表情包库现在是静态的**：被动收集没了，这个库不会再增长。迁移时从旧部署复制了 1404 张（见下）。

---

## 🏗 项目结构

```
KirikoBot/
├── main.py                # Flask 入口，路由注册，服务编排
├── qq_official.py         # ★ QQ 官方平台客户端（token 管理 / 发消息 / 撤回 / MessageBuilder）
├── qq_gateway.py          # ★ WebSocket 网关（心跳、断线重连、事件分发）
├── ai_server.py           # DeepSeek AI 请求
├── ai_tools.py            # 工具实现
├── ai_tools_list.py       # AI Function Calling 工具定义
├── robot_server.py        # 事件解析 → 机器人消息
├── config.py              # 环境变量配置
├── database_manager.py    # SQLite 数据库管理
├── prompt_builder.py      # 人设 PERSONA（唯一来源）+ 提示词拼装
├── chat_history.py        # 对话历史读写
├── feature_gate.py        # 功能开关（按群/全局）
├── scheduler.py           # 后台任务（保留清理/备份等，定时推送已删）
├── version_manager.py     # 版本号 + 变更日志（推送已删）
├── music_service.py       # 网易云音乐搜索
├── weather_service.py     # 天气查询
├── balance_service.py     # DeepSeek 余额查询
├── profile_service.py     # 用户画像分析
├── learning_service.py    # 自学习反馈
├── affection_service.py   # 好感度
├── judge_service.py       # 判定（是否搭话等）
├── extra_services.py      # 骰子、B站热搜、吃什么等
├── hot_news.py            # 热点新闻抓取
├── news_crawler.py        # 新闻爬虫
├── political_news.py      # 时政新闻（BBC/VOA）
├── sticker_collector.py   # 表情包库读取 / pHash 索引
├── web_search.py          # 联网搜索
├── ai_metrics.py          # AI 调用可观测性（token/延迟/缓存命中）
├── log_stream.py          # SSE 实时日志流
├── dashboard_auth.py      # 面板 HTTP Basic 鉴权
├── maintenance_service.py # 数据保留 / 备份
├── .env.example           # 环境变量模板
├── VERSION                # 当前版本号
├── requirements.txt       # Python 依赖
├── Dockerfile             # Docker 镜像构建
├── stickers/              # 表情包素材（git 只跟踪随源码发布的 756 张）
├── carside_picture/       # 塔罗牌面图素材（tarot_content.card_path 指向这里）
├── static/
│   ├── css/app.css        # 面板设计系统（日夜主题）
│   └── js/app.js          # 面板前端逻辑
└── templates/
    └── dashboard.html     # Web 管理面板外壳
```

`tools/` 下面是运维脚本，不属于运行时：

| 脚本 | 用途 |
|---|---|
| `tools/qq_probe.py` | 官方平台连通性探测：拿 token、看网关、抓原始事件、试被动回复 |
| `tools/migrate_data.py` | 从旧 OneBot 部署迁移参考数据 |

---

## 🖥 管理面板

打开 `http://localhost:5000`，支持日间 / 夜间主题，纯本地资源、无外部 CDN 依赖。

| 分组 | 页面 |
|------|------|
| 概览 | 总览、实时日志 |
| 对话与用户 | 对话记录、用户画像、好感度、自学习 |
| 内容与工具 | 塔罗、表情包、箱头库、功能清单 |
| 系统 | 版本日志、群管理、AI 用量、群设置 |
| 连接 | 连接状态 |

**删除群聊**：「群管理」页每个群都有删除按钮，会**永久清除该群全部数据**
（对话记录、用户画像、好感度、工具调用、功能需求、学习笔记、功能开关），
弹窗会先列出各项条数并要求输入群号确认。「同时让机器人退出该群」会调用官方接口退群。
表情包图库是全局共享的，不会被删除。

### 🔐 访问口令

面板可以删除数据、操作机器人，所以默认启用 **HTTP Basic 鉴权**：

- 首次启动自动生成口令，保存在 `KirikoBot/.dashboard_password`（已 gitignore），
  同时打印在启动日志里：`docker logs kirikobot-qq | grep 面板密码`
- 想自己指定就设 `DASHBOARD_USER` / `DASHBOARD_PASSWORD`
- 浏览器只需登录一次，之后同源的图片、SSE 日志流都会自动带上凭据

⚠️ Basic 认证在纯 HTTP 下只等于「网络有多私密就有多安全」。**不要把 5000 端口
直接暴露到公网**；确需外部访问请套一层 HTTPS 反向代理。

### 💾 数据保留与备份

- 数据库默认保留 **180 天**（`RETENTION_DAYS`，0 = 永久）
- 每天自动备份到 `KirikoBot/backups/`，保留最近 14 份（`BACKUP_KEEP`），
  使用 SQLite 在线备份 API，WAL 模式下同样安全

---

## 🗃 数据迁移

换到官方平台后，群和用户换成了 `group_openid` / `user_openid` —— 和 QQ 号是两套完全不同的 ID 空间，
**旧的聊天记录、画像、好感度、学习日志、塔罗历史都对不上任何人**，所以一律不迁。

迁移脚本只搬「和具体是谁无关」的参考数据：

```bash
# 先看会迁什么，不写任何东西
python3 tools/migrate_data.py --source /path/to/old/robot.db --dry-run

# 真迁（顺带把旧库里本地缺的贴图文件也复制过来）
python3 tools/migrate_data.py --source /path/to/old/robot.db --copy-stickers
```

| 迁移 | 说明 |
|---|---|
| `tarot_content` | 44 张塔罗牌的牌义文案 |
| `amp_heads` | 77 条吉他箱头资料（爬虫已删，这是唯一来源） |
| `app_versions` + `changelog` | 版本号与 93 条版本日志正文 |
| `stickers` | 表情包索引，只迁本地确实有文件的条目 |

`ai_calls`（AI 用量历史）默认不迁——它带旧群号，迁过来会在用量页上多出一批显示不出名字的幽灵群。
确实想留住历史曲线的话加 `--include-usage`。

脚本是幂等的，`--force` 会先清空目标库里这几张参考表再重灌，不碰任何其他表。

---

## 🧪 测试

```bash
pip install -r KirikoBot/requirements-dev.txt
python -m pytest tests/ -q
```

GitHub Actions 会在 PR 上自动跑测试与语法检查。

---

## 🔧 开发指南

新增工具需修改 4 个文件：`ai_tools_list.py`（schema）→ `ai_tools.py`（实现）→
`main.py`（导入 + 注册路由 + 实例化）→ `feature_gate.py`（`FEATURE_DEFS` + `TOOL_FEATURE`）。

漏掉任何一处都会静默失效或直接崩，测试里有针对这四处的静态检查。

### 常见问题

| 症状 | 原因 | 解决 |
|------|------|------|
| 面板「连接状态」一直显示未连接 | `QQ_APP_SECRET` 没填或填错 | 检查 `KirikoBot/.env` 并 `--force-recreate` |
| 机器人不回话 | 只响应 @ 和私聊；沙箱环境下非测试成员无效 | 确认 @ 了机器人；确认账号在沙箱测试成员里 |
| 同一条消息回了几次就不再回 | 官方限制每条消息最多回 5 次 | 正常现象 |
| 回复发不出去 | 超过 5 分钟被动回复窗口 | 正常现象，重新发一条 |
| 容器 exit 137 | OOM / SIGKILL | 重启容器 |
| 面板打开是 401 | 启用了 Basic 鉴权 | 见「🔐 访问口令」 |

---

## 📄 许可证

[GPL V3](LICENSE) © 2026 Bosak

---

## 🙏 致谢

- [QQ 开放平台](https://q.qq.com) – 官方机器人接口
- [DeepSeek](https://www.deepseek.com/) – 大语言模型 API
- [LLOneBot / LuckyLilliaBot](https://github.com/LLOneBot/LuckyLilliaBot) – 上游分线所用的 QQ 机器人框架
