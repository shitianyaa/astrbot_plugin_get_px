from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CHECKIN_SNAPSHOT_SCHEMA_VERSION = 5
CHECKIN_SNAPSHOT_SCOPE = "checkin"
CHECKIN_SNAPSHOT_PLUGIN_NAME = "astrbot_plugin_get_px"

BASE_COIN_MIN = 50
BASE_COIN_MAX = 100
BASE_AFFECTION_MIN = 0.50
BASE_AFFECTION_MAX = 1.20
STREAK_STEP_DAYS = 7
STREAK_COIN_BONUS = 10
STREAK_COIN_BONUS_MAX = 50
STREAK_AFFECTION_BONUS = 0.10
STREAK_AFFECTION_BONUS_MAX = 0.50
BOOST_MULTIPLIER = 2.0
MIN_AFFECTION = -10.0

BOOST_PRODUCTS: dict[int, int] = {
    1: 200,
    3: 500,
    7: 1000,
}

ACHIEVEMENTS: dict[str, dict[str, Any]] = {
    "first_meeting": {"title": "初见旅人", "kind": "total", "threshold": 1},
    "streak_7": {"title": "七日同行", "kind": "streak", "threshold": 7},
    "total_30": {"title": "月下常客", "kind": "total", "threshold": 30},
    "total_100": {"title": "百日珍藏", "kind": "total", "threshold": 100},
    "total_365": {"title": "周年相守", "kind": "total", "threshold": 365},
    "total_1000": {"title": "千日物语", "kind": "total", "threshold": 1000},
}


@dataclass(frozen=True)
class CheckinProfile:
    user_id: str
    coins: int
    affection: float
    total_days: int
    streak_days: int
    last_checkin_date: str
    boost_start_date: str
    boost_until_date: str
    repeat_penalty_date: str
    repeat_penalty_total: float
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CheckinRecord:
    date_key: str
    user_id: str
    username: str
    bot_name: str
    base_coins: int
    bonus_coins: int
    coins_reward: int
    base_affection: float
    bonus_affection: float
    affection_reward: float
    boost_active: bool
    boost_multiplier: float
    total_coins_after: int
    total_affection_after: float
    total_days_after: int
    streak_days_after: int
    note: str
    background_mode: str
    background_source: str
    background_illust_id: str
    background_title: str
    background_author: str
    created_at: str
    updated_at: str
    event_key: str = ""
    event_label: str = ""
    greeting: str = ""
    greeting_source: str = "local"
    greeting_attribution: str = ""
    secondary_note: str = ""
    template_version: str = "v2"
    theme_id: str = "default"


@dataclass(frozen=True)
class CheckinResult:
    profile: CheckinProfile
    record: CheckinRecord | None
    duplicate: bool
    penalty_amount: float = 0.0
    penalty_total_today: float = 0.0


@dataclass(frozen=True)
class BoostPurchaseResult:
    success: bool
    profile: CheckinProfile
    days: int
    cost: int
    message: str


@dataclass(frozen=True)
class ThemePurchaseResult:
    success: bool
    profile: CheckinProfile
    theme_id: str
    cost: int
    already_owned: bool
    message: str


@dataclass(frozen=True)
class BackgroundRefreshResult:
    success: bool
    profile: CheckinProfile
    record: CheckinRecord | None
    cost: int
    message: str


@dataclass(frozen=True)
class CheckinUserPreference:
    user_id: str
    birthday_month: int
    birthday_day: int
    birthday_source: str
    qq_birthday_checked: bool
    selected_title_id: str
    created_at: str
    updated_at: str
    selected_theme_id: str = "default"

    @property
    def birthday_label(self) -> str:
        if self.birthday_month <= 0 or self.birthday_day <= 0:
            return ""
        return f"{self.birthday_month:02d}-{self.birthday_day:02d}"


@dataclass(frozen=True)
class CheckinGlobalEvent:
    event_id: int
    event_type: str
    date_value: str
    name: str
    created_by: str
    created_at: str
    updated_at: str
