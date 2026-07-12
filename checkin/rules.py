from __future__ import annotations

from datetime import date, datetime
import hashlib
import random
from typing import Any

from .models import (
    BASE_AFFECTION_MAX,
    BASE_AFFECTION_MIN,
    BASE_COIN_MAX,
    BASE_COIN_MIN,
    MIN_AFFECTION,
    SHANGHAI_TZ,
    CheckinProfile,
)


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
    seed_text = f"checkin-reward|{date_key}|{user_id}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    coins = rng.randint(BASE_COIN_MIN, BASE_COIN_MAX)
    affection = round(rng.uniform(BASE_AFFECTION_MIN, BASE_AFFECTION_MAX), 2)
    return coins, affection


def daily_note(user_id: str, date_key: str, streak_days: int) -> str:
    notes = [
        "明天也要来哦",
        "今天也有好好见面",
        "脚步很轻，但确实在靠近",
        "连续记录被认真收好了",
        "今天的心情也闪了一下",
    ]
    if streak_days and streak_days % 7 == 0:
        return f"连续 {streak_days} 天，奖励加成已生效"
    seed_text = f"checkin-note|{date_key}|{user_id}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    return notes[seed % len(notes)]


def parse_date(value: str) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
