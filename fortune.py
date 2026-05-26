from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import random


@dataclass(frozen=True)
class FortuneResult:
    username: str
    date_str: str
    title: str
    star_count: int
    max_stars: int
    description: str
    good: str
    bad: str
    extra_message: str


_FORTUNE_LEVELS = [
    ("大吉", 7, "今天气场很顺，适合主动推进想做的事。"),
    ("中吉", 6, "今天整体顺利，认真一点就会有不错收获。"),
    ("小吉", 5, "今天会有一些小惊喜，保持平常心就很好。"),
    ("吉", 4, "今天平稳向好，适合把日常节奏整理清楚。"),
    ("末吉", 3, "今天需要多一点耐心，慢慢来也能稳住局面。"),
    ("小凶", 2, "今天容易被小事打断，先照顾好自己的状态。"),
    ("凶", 1, "今天适合低调一点，重要决定可以再观察一下。"),
]

_GOOD_ITEMS = [
    "摸鱼",
    "补番",
    "早睡",
    "喝水",
    "散步",
    "晒太阳",
    "整理房间",
    "听歌",
    "看电影",
    "读书",
    "做计划",
    "记账",
    "做饭",
    "点奶茶",
    "吃甜食",
    "和朋友聊天",
    "表达感谢",
    "出门走走",
    "泡澡",
    "护肤",
    "运动",
    "拍照",
    "收拾桌面",
    "清空待办",
    "尝试新东西",
    "画画",
    "写日记",
    "逛超市",
    "买小礼物",
    "看 Pixiv",
    "约饭",
    "学习新东西",
]

_BAD_ITEMS = [
    "冲动消费",
    "熬夜",
    "空腹喝咖啡",
    "暴饮暴食",
    "情绪上头",
    "冷落消息",
    "拖到最后一刻",
    "把话说太满",
    "临时改计划",
    "忘带钥匙",
    "忘记充电",
    "久坐不动",
    "憋着不说",
    "反复纠结",
    "盲目答应",
    "过度比较",
    "乱买没用小东西",
    "边走边看手机",
    "饭后立刻躺下",
    "把小事想复杂",
    "带着脾气沟通",
    "临睡前刷太久",
    "连续刷新",
    "拖延",
    "硬撑疲惫",
]

_EXTRA_MESSAGES = [
    "顺手做一件小事，今天会轻松很多。",
    "遇到卡点先休息五分钟，答案可能会慢慢浮出来。",
    "今天适合相信直觉，但也别忘了给自己留余地。",
    "把复杂的事情拆小一点，今天就会好过很多。",
    "先照顾好情绪，再处理事情，效率会更稳。",
    "不用急着证明什么，按自己的节奏来就好。",
    "今天适合主动一点，机会可能藏在普通对话里。",
    "给生活加一点仪式感，小事也会变得可爱。",
    "少看一点别人的进度，多确认一下自己的方向。",
    "少一点纠结，多一点行动。",
]


def build_fortune(
    user_id: str, username: str, group_id: str | None = None
) -> FortuneResult:
    date_str = date.today().isoformat()
    seed_text = f"{date_str}|{user_id}|{group_id or 'private'}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)

    title, star_count, description = rng.choice(_FORTUNE_LEVELS)
    good = "、".join(rng.sample(_GOOD_ITEMS, 3))
    bad = "、".join(rng.sample(_BAD_ITEMS, 3))
    extra_message = rng.choice(_EXTRA_MESSAGES)

    return FortuneResult(
        username=username or user_id or "用户",
        date_str=date_str,
        title=title,
        star_count=star_count,
        max_stars=7,
        description=description,
        good=good,
        bad=bad,
        extra_message=extra_message,
    )


def format_fortune(result: FortuneResult) -> str:
    stars = "★" * result.star_count + "☆" * (result.max_stars - result.star_count)
    return "\n".join(
        [
            f"{result.username} 的今日运势 ({result.date_str})",
            f"运势：{result.title}",
            f"星级：{stars}",
            f"说明：{result.description}",
            f"宜：{result.good}",
            f"忌：{result.bad}",
            result.extra_message,
        ]
    )
