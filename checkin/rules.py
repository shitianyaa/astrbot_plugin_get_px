from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import math
import random
from typing import Any

from .models import (
    BASE_AFFECTION_MAX,
    BASE_AFFECTION_MIN,
    BASE_COIN_MAX,
    BASE_COIN_MIN,
    MIN_AFFECTION,
    SHANGHAI_TZ,
    STREAK_AFFECTION_BONUS,
    STREAK_AFFECTION_BONUS_MAX,
    STREAK_COIN_BONUS,
    STREAK_COIN_BONUS_MAX,
    STREAK_STEP_DAYS,
    CheckinProfile,
)


@dataclass(frozen=True)
class CheckinRewardSettings:
    coin_min: int = BASE_COIN_MIN
    coin_max: int = BASE_COIN_MAX
    streak_step_days: int = STREAK_STEP_DAYS
    streak_coin_bonus: int = STREAK_COIN_BONUS
    streak_coin_bonus_max: int = STREAK_COIN_BONUS_MAX
    streak_affection_bonus: float = STREAK_AFFECTION_BONUS
    streak_affection_bonus_max: float = STREAK_AFFECTION_BONUS_MAX


_REWARD_BOUNDS = {
    "coin_min": (int, 0, 10000, BASE_COIN_MIN),
    "coin_max": (int, 0, 10000, BASE_COIN_MAX),
    "streak_step_days": (int, 1, 365, STREAK_STEP_DAYS),
    "streak_coin_bonus": (int, 0, 10000, STREAK_COIN_BONUS),
    "streak_coin_bonus_max": (int, 0, 100000, STREAK_COIN_BONUS_MAX),
    "streak_affection_bonus": (float, 0, 100, STREAK_AFFECTION_BONUS),
    "streak_affection_bonus_max": (float, 0, 1000, STREAK_AFFECTION_BONUS_MAX),
}


def normalize_reward_settings(
    raw: dict[str, object],
) -> tuple[CheckinRewardSettings, tuple[str, ...]]:
    """Normalize config values without mutation; return invalid field/reason diagnostics."""
    values: dict[str, object] = {}
    invalid: list[str] = []
    for name, (kind, low, high, default) in _REWARD_BOUNDS.items():
        value = raw.get(name, default)
        valid_type = type(value) is int if kind is int else type(value) in (int, float)
        try:
            numeric = float(value) if valid_type else float("nan")
        except (TypeError, ValueError, OverflowError):
            numeric = float("nan")
        if not valid_type or not math.isfinite(numeric) or not low <= numeric <= high:
            invalid.append(f"{name}:invalid")
            values[name] = default
        else:
            values[name] = int(value) if kind is int else float(value)
    if values["coin_min"] > values["coin_max"]:
        invalid.append("coin_min/coin_max:reversed")
        values["coin_min"], values["coin_max"] = BASE_COIN_MIN, BASE_COIN_MAX
    return CheckinRewardSettings(**values), tuple(invalid)


def _reward_seed(user_id: str, date_key: str) -> int:
    seed_text = f"checkin-reward|{date_key}|{user_id}"
    return int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")


def deterministic_base_coins(
    user_id: str, date_key: str, coin_min: int, coin_max: int
) -> int:
    return random.Random(_reward_seed(user_id, date_key)).randint(coin_min, coin_max)


def affection_level(value: float) -> dict[str, Any]:
    levels = [
        (MIN_AFFECTION, 0.0, "排斥"),
        (0.0, 10.0, "陌生"),
        (10.0, 30.0, "熟悉"),
        (30.0, 70.0, "亲近"),
        (70.0, 140.0, "信赖"),
        (140.0, None, "挚友"),
    ]
    for lower, upper, name in levels:
        if upper is None or value < upper:
            if upper is None:
                progress = 100
                next_value = None
            else:
                span = upper - lower
                progress = int(max(0, min(100, ((value - lower) / span) * 100)))
                next_value = upper
            return {
                "name": name,
                "lower": lower,
                "upper": upper,
                "next": next_value,
                "progress": progress,
            }
    return {
        "name": "挚友",
        "lower": 140.0,
        "upper": None,
        "next": None,
        "progress": 100,
    }


def _today_key() -> str:
    return datetime.now(SHANGHAI_TZ).date().isoformat()


def is_boost_active(profile: CheckinProfile, date_key: str | None = None) -> bool:
    today = date.fromisoformat(date_key or _today_key())
    start = parse_date(profile.boost_start_date)
    until = parse_date(profile.boost_until_date)
    return bool(start and until and start <= today <= until)


def boost_remaining_days(profile: CheckinProfile, date_key: str | None = None) -> int:
    today = date.fromisoformat(date_key or _today_key())
    until = parse_date(profile.boost_until_date)
    if until is None or until < today:
        return 0
    return (until - today).days + 1


def boost_status_text(profile: CheckinProfile, date_key: str | None = None) -> str:
    today = date.fromisoformat(date_key or _today_key())
    start = parse_date(profile.boost_start_date)
    until = parse_date(profile.boost_until_date)
    if start is None or until is None or until < today:
        return "无加持"
    remaining = (until - max(today, start)).days + 1
    if start > today:
        return f"{start.isoformat()} 生效，剩余 {remaining} 天"
    return f"生效中，剩余 {remaining} 天"


def daily_base_reward(user_id: str, date_key: str) -> tuple[int, float]:
    seed = _reward_seed(user_id, date_key)
    rng = random.Random(seed)
    coins = rng.randint(BASE_COIN_MIN, BASE_COIN_MAX)
    affection = round(rng.uniform(BASE_AFFECTION_MIN, BASE_AFFECTION_MAX), 2)
    return coins, affection


def daily_note(
    user_id: str,
    date_key: str,
    streak_days: int,
    *,
    interval: int = STREAK_STEP_DAYS,
) -> str:
    notes = [
        "明天也要来哦",
        "今天也有好好见面",
        "脚步很轻，但确实在靠近",
        "连续记录被认真收好了",
        "今天的心情也闪了一下",
    ]
    if streak_days and streak_days % interval == 0:
        return f"连续 {streak_days} 天，奖励加成已生效"
    seed_text = f"checkin-note|{date_key}|{user_id}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    return notes[seed % len(notes)]


def checkin_coin_bonus(
    *,
    base_coins: int,
    streak_days: int,
    affection: float,
    step_percent: int = 0,
    enabled: bool = True,
    reward_settings: CheckinRewardSettings | None = None,
) -> int:
    """Return streak and pre-checkin affection-level coin bonuses."""
    policy = reward_settings or CheckinRewardSettings()
    streak_bonus = min(
        (streak_days // policy.streak_step_days) * policy.streak_coin_bonus,
        policy.streak_coin_bonus_max,
    )
    if not enabled or not step_percent:
        return streak_bonus
    level_steps = {
        "排斥": 0,
        "陌生": 0,
        "熟悉": 1,
        "亲近": 2,
        "信赖": 3,
        "挚友": 4,
    }.get(affection_level(affection)["name"], 0)
    return streak_bonus + math.floor(
        (base_coins + streak_bonus) * level_steps * step_percent / 100
    )


def checkin_affection_bonus(streak_days: int, *, reward_settings: CheckinRewardSettings | None = None) -> float:
    """Return the unchanged streak affection bonus."""
    policy = reward_settings or CheckinRewardSettings()
    return min(
        (streak_days // policy.streak_step_days) * policy.streak_affection_bonus,
        policy.streak_affection_bonus_max,
    )


def parse_date(value: str) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
