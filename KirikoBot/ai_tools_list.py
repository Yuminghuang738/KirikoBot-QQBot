from __future__ import annotations

from typing import Any


class AiTools:
    def ai_tools(self) -> list[dict[str, Any]]:
        empty_params: dict[str, Any] = {
            "type": "object",
            "properties": {},
            "required": [],
        }
        function_tarot = {
            "name": "tarot",
            "description": "当用户表示想要进行占卜、算命、抽塔罗牌、测运势时调用。用户可能要求给自己抽或给群友抽",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {
                        "type": "string",
                        "description": "要给谁抽牌，默认是当前用户。如果用户说'给XXX抽一张'，则target_name=XXX",
                    }
                },
                "required": [],
            },
        }
        function_tarot_history = {
            "name": "tarot_history",
            "description": "当用户表示想要知道自己占卜,算命或询问运势结果或塔罗牌抽取的历史记录,才调用此函数",
            "parameters": empty_params,
        }
        function_gaming_news = {
            "name": "gaming_news",
            "description": "当用户表示想要获取游戏新闻、热点游戏资讯、游戏行业动态、游戏圈最新消息时，调用此函数获取最新的热点游戏新闻",
            "parameters": empty_params,
        }
        function_web_search = {
            "name": "web_search",
            "description": "当用户询问实时信息、最新资讯、需要联网查询的问题、或者你不知道答案且需要搜索时，调用此函数进行联网搜索",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，用中文或英文均可",
                    }
                },
                "required": ["query"],
            },
        }
        function_weather = {
            "name": "weather",
            "description": "当用户询问天气、气温、是否会下雨、穿什么衣服等天气相关问题时，调用此函数查询天气",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如：北京、上海、广州、深圳、成都等",
                    }
                },
                "required": ["city"],
            },
        }
        function_sticker = {
            "name": "sticker",
            "description": (
                "发送Kiriko表情包。根据当前聊天语境和氛围，选择合适的表情包分类发送。"
                "可用分类：可爱（开心/温暖时）、搞笑（幽默/整活时）、生气（不满/吐槽时）、"
                "惊讶（震惊/意外时）、悲伤（难过/安慰时）、打招呼（问候/欢迎时）、"
                "鼓励（加油/打气时）、庆祝（恭喜/祝贺时）、动物、动漫、其他。"
                "当对方表达情绪或聊天氛围适合用表情包回应时，自主选择匹配的分类。"
                "注意：当用户明确要求其他功能（新闻/天气/搜索/塔罗等）时，禁止用表情包替代。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "表情包分类，可选：可爱、搞笑、生气、惊讶、悲伤、打招呼、鼓励、庆祝、动物、动漫、其他。留空则随机发送",
                        "enum": ["可爱", "搞笑", "生气", "惊讶", "悲伤", "打招呼", "鼓励", "庆祝", "动物", "动漫", "其他", ""],
                    },
                },
                "required": [],
            },
        }
        function_request_sticker = {
            "name": "request_sticker",
            "description": (
                "当用户想给你看一张图片/表情包、想让你看/评价某张图（例如说“帮我看看这个图”“这张图怎么样”"
                "“我给你看个好东西”“看下我的表情包”），但当前这条消息并没有附带图片时，调用此函数。"
                "调用后系统会进入等待图片状态（30秒），你随后要用 Kiriko 的语气请对方把图片发过来。"
                "注意：这不是发送表情包给用户（那是 sticker 工具）；用户只是闲聊时提到“图片/表情包”这个词、"
                "或当前消息已经带了图片时，都不要调用。"
            ),
            "parameters": empty_params,
        }
        function_hitokoto = {
            "name": "hitokoto",
            "description": "当用户表示想听一句话、来句名言、励志语录、每日一句、一言时，调用此函数获取随机一言",
            "parameters": empty_params,
        }
        function_food = {
            "name": "food_picker",
            "description": "当用户询问吃什么、今天吃什么、推荐美食、不知道吃啥、帮忙选吃的时，调用此函数随机推荐食物",
            "parameters": empty_params,
        }
        function_dice = {
            "name": "dice",
            "description": "当用户要求掷骰子、roll点、随机数、抽签决定时，调用此函数掷骰子",
            "parameters": {
                "type": "object",
                "properties": {
                    "sides": {
                        "type": "integer",
                        "description": "骰子面数，默认6面，可选：6(默认), 20(D20), 100(D100)等",
                    }
                },
                "required": [],
            },
        }
        function_bilibili = {
            "name": "bilibili_trending",
            "description": "当用户询问B站热搜、B站热门、bilibili热搜、B站排行、B站视频排行时，调用此函数获取B站热搜榜单",
            "parameters": empty_params,
        }
        function_political_news = {
            "name": "political_news",
            "description": "当用户询问时政新闻、国际新闻、政治新闻、全球时事、BBC新闻、最新时事等时调用，获取权威媒体的时政新闻",
            "parameters": empty_params,
        }
        function_balance = {
            "name": "check_balance",
            "description": "当用户询问DeepSeek余额、API余额、账户余额、还剩多少钱、额度还剩多少时，调用此函数查询DeepSeek账户余额",
            "parameters": empty_params,
        }
        function_current_time = {
            "name": "get_current_time",
            "description": "获取当前精确时间（精确到秒），用于回答“现在几点”“今天几号”以及计算相对时间如'30秒后'、'5分钟后'",
            "parameters": empty_params,
        }
        function_feature_request = {
            "name": "submit_feature",
            "description": "当用户提出功能建议、想要新功能、或者说'建议'、'希望能'、'能不能加'、'要是能'等时调用。记录群友的功能需求到待办清单",
            "parameters": {
                "type": "object",
                "properties": {
                    "request": {
                        "type": "string",
                        "description": "用户的功能请求原文，保持用户原话",
                    }
                },
                "required": ["request"],
            },
        }
        function_music = {
            "name": "music_search",
            "description": "当用户表示想点歌、放歌、来首歌、搜歌、放音乐、播放歌曲、点一首、我想听、放一首歌时，调用此函数搜索并播放歌曲。用户可能说：点一首XXX、放歌XXX、来首XXX的歌、我想听XXX等。重要：一次只调用一次，即使用户说了多首歌也只搜一首，把所有关键词合并成一个搜索词（如用户说'晴天和稻香'则搜'晴天 稻香'）",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词，把所有歌名和歌手合并成一个字符串，如用户说'周杰伦的晴天和稻香'则传'周杰伦 晴天 稻香'。不要拆分成多次调用。",
                    }
                },
                "required": ["keyword"],
            },
        }
        function_sticker_battle = {
            "name": "sticker_battle",
            "description": "当用户表示想要斗图、表情包对战、贴纸大战、PK表情包、来互相伤害、发起表情包挑战时调用。启动斗图模式，机器人会先发一张表情包发起挑战，然后多轮回合对决",
            "parameters": empty_params,
        }
        function_check_affection = {
            "name": "check_affection",
            "description": "当用户询问好感度、查看好感、查好感、我的好感、谁最喜欢我、和我的关系、查看关系时调用。查询用户与机器人的好感度数值和关系评价",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_name": {
                        "type": "string",
                        "description": "要查询好感度的群友昵称。留空或说'我'则查询当前用户自己的好感度",
                    }
                },
                "required": [],
            },
        }
        function_affection_leaderboard = {
            "name": "affection_leaderboard",
            "description": "当用户询问好感度排行、好感排行榜、谁的好感度最高、好感度排名、谁最喜欢机器人时调用。返回当前群的好感度排行榜",
            "parameters": empty_params,
        }
        function_recall_message = {
            "name": "recall_message",
            "description": (
                "撤回你自己刚发出去的那条消息。当用户说「撤回」「收回刚才那句」「说错了」"
                "「当我没说」时调用；你发现自己上一条回复明显不合适时也可以调用。"
                "只能撤回你自己发出的、且在两分钟内的消息，超时会失败"
            ),
            "parameters": empty_params,
        }
        function_feature_list = {
            "name": "feature_list",
            "description": (
                "查询群友提交过的功能需求清单及其处理状态。"
                "当用户问「还有什么功能没做」「之前提的需求怎么样了」「待完成的功能有哪些」时调用"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "筛选状态：pending（待处理，默认）、done（已完成）、rejected（已拒绝）、all（全部）",
                    },
                },
                "required": [],
            },
        }
        function_explain_self = {
            "name": "explain_self",
            "description": (
                "调试用途：把上一轮的原始记录（思维链原文、工具调用、最终回复）原文照录地发到当前对话里。"
                "当用户问「你刚才干了什么」「你调用什么工具了」「你刚才在想什么」「怎么做到的」时调用。"
                "调用后会直接把原文发出去，你不需要再用自己的话复述或解释，也不要在后面补一句总结——"
                "用户要看的就是未经转述的原文。"
            ),
            "parameters": empty_params,
        }
        function_similar_sticker = {
            "name": "similar_sticker",
            "description": (
                "当用户发了一张图/表情包并想找类似的时调用"
                "（例如「有没有类似的」「来个同款表情」）。会从表情库里挑最像的一张发出来"
            ),
            "parameters": empty_params,
        }

        function_amp_head = {
            "name": "amp_head",
            "description": (
                "当用户想了解/推荐吉他音箱箱头（amp head）时调用，"
                "例如「推荐个箱头」「今天弹什么箱头」「有什么经典的电子管箱头」。"
                "会从箱头资料库里给出一条，含年份/功率/电子管/音色/参考价等。"
                "注意：这只是资料推荐，不是购买链接"
            ),
            "parameters": empty_params,
        }

        tool_tarot = {"type": "function", "function": function_tarot}
        tool_tarot_history = {"type": "function", "function": function_tarot_history}
        tool_gaming_news = {"type": "function", "function": function_gaming_news}
        tool_web_search = {"type": "function", "function": function_web_search}
        tool_weather = {"type": "function", "function": function_weather}
        tool_sticker = {"type": "function", "function": function_sticker}
        tool_request_sticker = {"type": "function", "function": function_request_sticker}
        tool_hitokoto = {"type": "function", "function": function_hitokoto}
        tool_food = {"type": "function", "function": function_food}
        tool_dice = {"type": "function", "function": function_dice}
        tool_political_news = {"type": "function", "function": function_political_news}
        tool_bilibili = {"type": "function", "function": function_bilibili}
        tool_feature_request = {"type": "function", "function": function_feature_request}
        tool_balance = {"type": "function", "function": function_balance}
        tool_current_time = {"type": "function", "function": function_current_time}
        tool_music = {"type": "function", "function": function_music}
        tool_sticker_battle = {"type": "function", "function": function_sticker_battle}
        tool_check_affection = {"type": "function", "function": function_check_affection}
        tool_affection_leaderboard = {"type": "function", "function": function_affection_leaderboard}
        tool_recall_message = {"type": "function", "function": function_recall_message}
        tool_feature_list = {"type": "function", "function": function_feature_list}
        tool_explain_self = {"type": "function", "function": function_explain_self}
        tool_similar_sticker = {"type": "function", "function": function_similar_sticker}
        tool_amp_head = {"type": "function", "function": function_amp_head}

        return [
            tool_tarot,
            tool_tarot_history,
            tool_gaming_news,
            tool_web_search,
            tool_weather,
            tool_sticker,
            tool_request_sticker,
            tool_hitokoto,
            tool_food,
            tool_dice,
            tool_political_news,
            tool_balance,
            tool_bilibili,
            tool_feature_request,
            tool_current_time,
            tool_music,
            tool_sticker_battle,
            tool_check_affection,
            tool_affection_leaderboard,
            tool_recall_message,
            tool_feature_list,
            tool_explain_self,
            tool_similar_sticker,
            tool_amp_head,
        ]
