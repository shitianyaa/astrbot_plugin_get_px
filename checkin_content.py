from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date

from lunar_python import Solar

from checkin import CheckinProfile, CheckinRecord
from holiday_calendar import OnlineHoliday

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

_EVENT_GREETINGS: dict[str, tuple[str, str, str]] = {
    "birthday": (
        "生日快乐，愿今天的好运安稳陪着你。",
        "生日快乐，很高兴能陪你记下今天。",
        "生日快乐，与你相遇是今天最珍贵的礼物。",
    ),
    "custom": (
        "{event}到了，愿这份纪念带给你温柔的好心情。",
        "又来到{event}，很高兴今天也与你相遇。",
        "{event}值得珍藏，而你让这一天更加特别。",
    ),
    "new_year": ("元旦快乐，愿新一年平安顺遂。", "新年又见面了，一起收好今天的祝福吧。", "新年的第一页，也想继续与你并肩写下。"),
    "spring_festival": ("春节快乐，愿新春安稳如意。", "新春相遇，愿团圆与好运都来到你身边。", "新岁与你重逢，就是我收到的最好祝福。"),
    "lantern_festival": ("元宵节快乐，愿灯火照亮你的归途。", "元宵相遇，记得收下今天这份圆满。", "万家灯火里，我依然最期待与你相见。"),
    "labour_day": ("劳动节快乐，也要记得好好休息。", "假日相遇，愿今天轻松又自在。", "辛苦之后的闲暇，想与你一起慢慢珍藏。"),
    "dragon_boat": ("端午安康，愿你今日顺遂。", "端午相遇，愿粽香与好运都陪着你。", "端午安康，与你相见让今日更值得纪念。"),
    "qixi": ("七夕快乐，愿今天有温柔相伴。", "七夕又见面了，愿你的心意都有回应。", "星河漫长，而我仍在这里等你如约而来。"),
    "mid_autumn": ("中秋快乐，愿你平安团圆。", "月圆之夜与你相遇，今天也圆满了。", "月色很好，而与你重逢让它更值得珍藏。"),
    "national_day": ("国庆节快乐，愿假日轻松顺利。", "国庆相遇，愿今天多一份从容好心情。", "在这个特别的日子与你见面，真好。"),
    "double_ninth": ("重阳安康，愿岁岁平安。", "重阳相遇，愿秋日温柔常伴你左右。", "又逢重阳，与你共记这一页便是珍贵。"),
    "workday": ("今天是调休工作日，也别忘了留一点休息时间。", "调休工作日也要照顾好自己，我们慢慢来。", "即使今天需要补班，我也想为你留一小段温柔。"),
}


def _event_greeting_bank(event_key: str) -> dict[str, tuple[str, ...]]:
    phrases = _EVENT_GREETINGS.get(event_key)
    if phrases is None:
        return _SPECIAL_GREETINGS
    return {
        "low": (phrases[0],),
        "mid": (phrases[1],),
        "high": (phrases[2],),
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
    secondary_events: str = ""
    current_title: str = ""
    unlocked_achievements: str = ""

    def to_plain_text(self) -> str:
        fields = (
            ("角色名", self.bot_name),
            ("用户昵称", self.username),
            ("日期", self.date_label),
            ("今日事件", self.event_label or "普通签到"),
            ("次要事件", self.secondary_events or "无"),
            ("关系阶段", self.relationship_stage),
            ("连续签到", f"{self.streak_days} 天"),
            ("累计签到", f"{self.total_days} 天"),
            ("今日奖励", f"金币 +{self.coins_reward}，好感度 +{self.affection_reward:g}"),
            ("里程碑", self.milestone or "无"),
            ("加持状态", self.boost_status or "无"),
            ("当前称号", self.current_title or "无"),
            ("今日解锁", self.unlocked_achievements or "无"),
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
    online_holiday: OnlineHoliday | None = None,
    secondary_event_labels: tuple[str, ...] = (),
    current_title: str = "",
    unlocked_achievements: tuple[str, ...] = (),
) -> CheckinContent:
    day = date.fromisoformat(record.date_key)
    birthday_label = _clean_plain(birthday_label)
    custom_event_label = _clean_plain(custom_event_label)
    milestone = _milestone(record.total_days_after)
    streak_event = record.streak_days_after > 0 and record.streak_days_after % 7 == 0
    built_in = _built_in_event(day)
    workday_label = ""
    if online_holiday and online_holiday.is_off_day and built_in is None:
        built_in = (
            "online_holiday",
            online_holiday.name,
            f"{online_holiday.name.removesuffix('节')}相遇",
        )
    elif online_holiday and not online_holiday.is_off_day:
        workday_label = "今日为调休工作日"

    candidates: list[tuple[str, str, str, str]] = []
    if birthday_label:
        candidates.append((
            "birthday",
            birthday_label,
            "生日纪念",
            "生日",
        ))
    if custom_event_label:
        candidates.append((
            "custom",
            custom_event_label,
            "纪念日相遇",
            custom_event_label,
        ))
    if built_in:
        key, label, built_in_title = built_in
        candidates.append((key, label, built_in_title, label))
    if milestone:
        candidates.append((
            "milestone",
            f"累计签到 {milestone} 天",
            "签到纪念",
            f"{milestone}天",
        ))
    if streak_event:
        candidates.append((
            "streak",
            f"连续签到 {record.streak_days_after} 天",
            "连续相遇",
            f"连签{record.streak_days_after}天",
        ))
    event_key, event_label, title, badge = (
        candidates[0] if candidates else ("normal", "", "今日签到", "")
    )

    stage = _relationship_stage(profile.affection)
    greeting_key = "workday" if event_key == "normal" and workday_label else event_key
    bank = _NORMAL_GREETINGS if greeting_key == "normal" else _event_greeting_bank(greeting_key)
    greeting = _stable_choice(
        bank[stage], record.user_id, record.date_key, greeting_key, stage
    ).format(event=event_label)

    badges: list[str] = []
    if milestone and event_key != "milestone":
        badges.append(f"{milestone}天")
    elif streak_event and event_key != "streak":
        badges.append(f"连签{record.streak_days_after}天")

    cleaned_secondary = [
        _clean_plain(label) for label in secondary_event_labels if _clean_plain(label)
    ]
    secondary_notes: list[str] = []
    if event_key == "custom":
        secondary_notes.extend(cleaned_secondary)
    for candidate in candidates[1:]:
        secondary_notes.append(candidate[1])
        if candidate[0] == "custom":
            secondary_notes.extend(cleaned_secondary)
    if workday_label:
        secondary_notes.append(workday_label)
    secondary_notes = list(dict.fromkeys(secondary_notes))

    context = GreetingContext(
        bot_name=_clean_plain(record.bot_name),
        username=_clean_plain(record.username),
        user_id_hint="anon-"
        + hashlib.sha256(record.user_id.encode("utf-8")).hexdigest()[:8],
        date_label=record.date_key,
        event_label=event_label,
        secondary_events=" · ".join(secondary_notes),
        relationship_stage=stage,
        streak_days=record.streak_days_after,
        total_days=record.total_days_after,
        coins_reward=record.coins_reward,
        affection_reward=record.affection_reward,
        milestone=f"累计签到 {milestone} 天" if milestone else "",
        boost_status=(
            f"好感度奖励 ×{record.boost_multiplier:g}" if record.boost_active else ""
        ),
        current_title=_clean_plain(current_title),
        unlocked_achievements="、".join(map(_clean_plain, unlocked_achievements)),
        local_greeting=_truncate(greeting),
    )
    return CheckinContent(
        title=title,
        event_key=event_key,
        event_label=event_label,
        greeting=_truncate(greeting),
        badges=tuple(badges[:1]),
        secondary_note=_truncate(" · ".join(secondary_notes)),
        context=context,
    )
