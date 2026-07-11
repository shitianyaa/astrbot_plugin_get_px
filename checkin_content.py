from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from lunar_python import Solar

from checkin import CheckinProfile, CheckinRecord

MAX_GREETING_LENGTH = 44
MILESTONES = (7, 30, 100, 365, 1000)

_SOLAR_EVENTS: dict[tuple[int, int], tuple[str, str, str]] = {
    (1, 1): ("new_year", "元旦", "新年相遇"),
    (5, 1): ("labour_day", "劳动节", "假日相遇"),
    (10, 1): ("national_day", "国庆节", "国庆相遇"),
}

_LUNAR_EVENTS: dict[str, tuple[str, str]] = {
    "春节": ("spring_festival", "新春相遇"),
    "元宵节": ("lantern_festival", "元宵相遇"),
    "端午节": ("dragon_boat", "端午相遇"),
    "七夕节": ("qixi", "七夕相遇"),
    "中秋节": ("mid_autumn", "中秋相遇"),
    "重阳节": ("double_ninth", "重阳相遇"),
}

_NORMAL_GREETINGS: dict[str, tuple[str, ...]] = {
    "low": (
        "签到已经记下了，愿你今天一切顺利。",
        "今日的记录完成了，请按自己的步调前进。",
        "奖励已收好，祝你度过平稳的一天。",
    ),
    "mid": (
        "又见面了，今天也一起把这份好运收好吧。",
        "签到完成，我已经开始期待明天再见了。",
        "今天的奖励准备好了，记得带着好心情出发。",
    ),
    "high": (
        "你如约而来，今天也成了值得珍藏的一页。",
        "能把今天与你一起记下，是只属于我们的纪念。",
        "欢迎回来，我一直为你留着今天的位置。",
    ),
}

_SPECIAL_GREETINGS: dict[str, tuple[str, ...]] = {
    "low": (
        "{event}快乐，今天的签到已经为你记下。",
        "恰逢{event}，愿这份小小奖励伴你顺利。",
        "今天是{event}，祝你度过安稳愉快的一天。",
    ),
    "mid": (
        "{event}快乐，很高兴今天也能在这里见到你。",
        "在{event}与你相遇，让今天多了一份好心情。",
        "把{event}和今日奖励一起收好，我们明天再见。",
    ),
    "high": (
        "{event}快乐，与你相遇让这个日子更值得纪念。",
        "今天是{event}，我想把这份温柔只留给你。",
        "能与你一起记住{event}，就是今天最好的礼物。",
    ),
}


def _clean_plain(value: object) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()


@dataclass(frozen=True)
class GreetingContext:
    bot_name: str
    username: str
    user_id_hint: str
    date_label: str
    event_label: str
    relationship_stage: str
    streak_days: int
    total_days: int
    coins_reward: int
    affection_reward: float
    milestone: str
    boost_status: str
    local_greeting: str

    def to_plain_text(self) -> str:
        fields = (
            ("角色名", self.bot_name),
            ("用户昵称", self.username),
            ("日期", self.date_label),
            ("今日事件", self.event_label or "普通签到"),
            ("关系阶段", self.relationship_stage),
            ("连续签到", f"{self.streak_days} 天"),
            ("累计签到", f"{self.total_days} 天"),
            ("今日奖励", f"金币 +{self.coins_reward}，好感度 +{self.affection_reward:g}"),
            ("里程碑", self.milestone or "无"),
            ("加持状态", self.boost_status or "无"),
        )
        return "\n".join(f"{name}：{_clean_plain(value)}" for name, value in fields)


@dataclass(frozen=True)
class CheckinContent:
    title: str
    event_key: str
    event_label: str
    greeting: str
    badges: tuple[str, ...]
    secondary_note: str
    context: GreetingContext


def _relationship_stage(affection: float) -> str:
    if affection < 10:
        return "low"
    if affection < 70:
        return "mid"
    return "high"


def _stable_choice(options: tuple[str, ...], *parts: object) -> str:
    digest = hashlib.sha256("\x1f".join(map(str, parts)).encode("utf-8")).digest()
    return options[int.from_bytes(digest[:8], "big") % len(options)]


def _truncate(text: str, limit: int = MAX_GREETING_LENGTH) -> str:
    clean = _clean_plain(text)
    return clean if len(clean) <= limit else clean[:limit]


def _built_in_event(day: date) -> tuple[str, str, str] | None:
    solar = _SOLAR_EVENTS.get((day.month, day.day))
    if solar:
        return solar
    festivals = Solar.fromYmd(day.year, day.month, day.day).getLunar().getFestivals()
    for festival in festivals:
        lunar = _LUNAR_EVENTS.get(festival)
        if lunar:
            key, title = lunar
            return key, festival, title
    return None


def _milestone(total_days: int) -> int | None:
    return total_days if total_days in MILESTONES else None


def resolve_checkin_content(
    record: CheckinRecord,
    profile: CheckinProfile,
    *,
    birthday_label: str = "",
    custom_event_label: str = "",
) -> CheckinContent:
    day = date.fromisoformat(record.date_key)
    birthday_label = _clean_plain(birthday_label)
    custom_event_label = _clean_plain(custom_event_label)
    milestone = _milestone(record.total_days_after)
    streak_event = record.streak_days_after > 0 and record.streak_days_after % 7 == 0
    built_in = _built_in_event(day)

    if birthday_label:
        event_key, event_label, title, badge = (
            "birthday",
            birthday_label,
            "生日纪念",
            "生日",
        )
    elif custom_event_label:
        event_key, event_label, title, badge = (
            "custom",
            custom_event_label,
            "纪念日相遇",
            custom_event_label,
        )
    elif built_in:
        event_key, event_label, title = built_in
        badge = event_label
    elif milestone:
        event_key, event_label, title, badge = (
            "milestone",
            f"累计签到 {milestone} 天",
            "签到纪念",
            f"{milestone}天",
        )
    elif streak_event:
        event_key, event_label, title, badge = (
            "streak",
            f"连续签到 {record.streak_days_after} 天",
            "连续相遇",
            f"连签{record.streak_days_after}天",
        )
    else:
        event_key, event_label, title, badge = "normal", "", "今日签到", ""

    stage = _relationship_stage(profile.affection)
    bank = _NORMAL_GREETINGS if event_key == "normal" else _SPECIAL_GREETINGS
    greeting = _stable_choice(
        bank[stage], record.user_id, record.date_key, event_key, stage
    ).format(event=event_label)

    badges: list[str] = []
    if badge:
        badges.append(_truncate(badge, 8))
    if milestone and event_key != "milestone":
        badges.append(f"{milestone}天")
    elif streak_event and event_key != "streak":
        badges.append(f"连签{record.streak_days_after}天")
    elif record.boost_active:
        badges.append(f"×{record.boost_multiplier:g}")

    secondary_notes: list[str] = []
    if milestone and event_key != "milestone":
        secondary_notes.append(f"累计签到达成 {milestone} 天")
    if streak_event and event_key not in {"streak", "milestone"}:
        secondary_notes.append(f"连续签到 {record.streak_days_after} 天")

    context = GreetingContext(
        bot_name=_clean_plain(record.bot_name),
        username=_clean_plain(record.username),
        user_id_hint="anon-"
        + hashlib.sha256(record.user_id.encode("utf-8")).hexdigest()[:8],
        date_label=record.date_key,
        event_label=event_label,
        relationship_stage=stage,
        streak_days=record.streak_days_after,
        total_days=record.total_days_after,
        coins_reward=record.coins_reward,
        affection_reward=record.affection_reward,
        milestone=f"累计签到 {milestone} 天" if milestone else "",
        boost_status=(
            f"好感度奖励 ×{record.boost_multiplier:g}" if record.boost_active else ""
        ),
        local_greeting=_truncate(greeting),
    )
    return CheckinContent(
        title=title,
        event_key=event_key,
        event_label=event_label,
        greeting=_truncate(greeting),
        badges=tuple(badges[:2]),
        secondary_note=_truncate("；".join(secondary_notes)),
        context=context,
    )
