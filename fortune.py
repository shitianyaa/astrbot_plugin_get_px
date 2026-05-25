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
    ("大吉", 7, "今天状态拉满，适合推进重要的事情。"),
    ("中吉", 6, "今天整体顺利，认真一点就会有不错结果。"),
    ("小吉", 5, "今天会有小惊喜，保持节奏就很好。"),
    ("吉", 4, "今天平稳向好，适合把手头事情做扎实。"),
    ("末吉", 3, "今天需要多一点耐心，慢慢来也能收获成果。"),
    ("小凶", 2, "今天容易被小事打断，先处理最重要的目标。"),
    ("凶", 1, "今天适合低调行事，重要决定可以再想想。"),
]

_GOOD_ITEMS = [
    "摸鱼",
    "写代码",
    "补番",
    "整理收藏",
    "早睡",
    "喝水",
    "散步",
    "清理缓存",
    "看 Pixiv",
    "重构",
    "备份",
    "学习新东西",
    "约饭",
    "画画",
]

_BAD_ITEMS = [
    "冲动消费",
    "熬夜",
    "空腹喝咖啡",
    "硬刚报错",
    "忘记保存",
    "乱改配置",
    "临时起意重装环境",
    "跳过文档",
    "连续刷新",
    "拖延",
    "带病加班",
    "深夜部署",
]

_EXTRA_MESSAGES = [
    "顺手做一件小事，今天会轻松很多。",
    "遇到卡点先休息五分钟，答案可能会自己冒出来。",
    "今天适合相信直觉，但别忘了保存进度。",
    "给自己留一点余量，运气更容易站在你这边。",
    "把复杂的事情拆小，今天就会很好过。",
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
