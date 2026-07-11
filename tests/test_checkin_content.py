from __future__ import annotations

from dataclasses import replace

from checkin import CheckinProfile, CheckinRecord
from checkin_content import resolve_checkin_content


def make_record(**overrides: object) -> CheckinRecord:
    values: dict[str, object] = {
        "date_key": "2026-07-11",
        "user_id": "10001",
        "username": "Alice",
        "bot_name": "neko",
        "base_coins": 80,
        "bonus_coins": 10,
        "coins_reward": 90,
        "base_affection": 0.8,
        "bonus_affection": 0.1,
        "affection_reward": 0.9,
        "boost_active": False,
        "boost_multiplier": 1.0,
        "total_coins_after": 900,
        "total_affection_after": 15.0,
        "total_days_after": 12,
        "streak_days_after": 3,
        "note": "",
        "background_mode": "",
        "background_source": "",
        "background_illust_id": "",
        "background_title": "",
        "background_author": "",
        "created_at": "2026-07-11T08:00:00+08:00",
        "updated_at": "2026-07-11T08:00:00+08:00",
    }
    values.update(overrides)
    return CheckinRecord(**values)  # type: ignore[arg-type]


def make_profile(**overrides: object) -> CheckinProfile:
    values: dict[str, object] = {
        "user_id": "10001",
        "coins": 900,
        "affection": 15.0,
        "total_days": 12,
        "streak_days": 3,
        "last_checkin_date": "2026-07-11",
        "boost_start_date": "",
        "boost_until_date": "",
        "repeat_penalty_date": "",
        "repeat_penalty_total": 0.0,
        "created_at": "2026-01-01T08:00:00+08:00",
        "updated_at": "2026-07-11T08:00:00+08:00",
    }
    values.update(overrides)
    return CheckinProfile(**values)  # type: ignore[arg-type]


def test_relationship_stages_have_distinct_local_text() -> None:
    record = make_record()

    low = resolve_checkin_content(record, make_profile(affection=5.0))
    mid = resolve_checkin_content(record, make_profile(affection=35.0))
    high = resolve_checkin_content(record, make_profile(affection=80.0))

    assert low.context.relationship_stage == "low"
    assert mid.context.relationship_stage == "mid"
    assert high.context.relationship_stage == "high"
    assert len({low.greeting, mid.greeting, high.greeting}) == 3


def test_local_selection_is_stable_by_user_and_date() -> None:
    record = make_record()
    profile = make_profile()

    first = resolve_checkin_content(record, profile)
    second = resolve_checkin_content(record, profile)
    variants = {
        resolve_checkin_content(
            replace(record, user_id=str(user_id), date_key=f"2026-07-{day:02d}"),
            replace(profile, user_id=str(user_id)),
        ).greeting
        for user_id, day in zip(range(10001, 10013), range(1, 13), strict=True)
    }

    assert first == second
    assert len(variants) > 1


def test_event_priority_birthday_then_custom_then_holiday() -> None:
    holiday = make_record(
        date_key="2026-10-01", total_days_after=30, streak_days_after=7
    )
    profile = make_profile(total_days=30, streak_days=7)

    birthday = resolve_checkin_content(
        holiday, profile, birthday_label="生日", custom_event_label="相遇纪念日"
    )
    custom = resolve_checkin_content(
        holiday, profile, custom_event_label="相遇纪念日"
    )
    built_in = resolve_checkin_content(holiday, profile)

    assert birthday.title == "生日纪念"
    assert birthday.event_key == "birthday"
    assert birthday.badges[0] == "生日"
    assert custom.event_key == "custom"
    assert custom.event_label == "相遇纪念日"
    assert built_in.event_key == "national_day"


def test_event_priority_holiday_then_milestone_then_streak_then_normal() -> None:
    profile = make_profile()

    holiday = resolve_checkin_content(
        make_record(date_key="2026-01-01", total_days_after=30, streak_days_after=7),
        profile,
    )
    milestone = resolve_checkin_content(
        make_record(total_days_after=30, streak_days_after=7), profile
    )
    streak = resolve_checkin_content(
        make_record(total_days_after=31, streak_days_after=7), profile
    )
    normal = resolve_checkin_content(
        make_record(total_days_after=31, streak_days_after=6), profile
    )

    assert holiday.event_key == "new_year"
    assert milestone.event_key == "milestone"
    assert streak.event_key == "streak"
    assert normal.event_key == "normal"


def test_known_lunar_festivals_are_resolved_with_lunar_python() -> None:
    profile = make_profile()

    spring_festival = resolve_checkin_content(
        make_record(date_key="2026-02-17"), profile
    )
    mid_autumn = resolve_checkin_content(
        make_record(date_key="2026-09-25"), profile
    )

    assert (spring_festival.event_key, spring_festival.event_label) == (
        "spring_festival",
        "春节",
    )
    assert (mid_autumn.event_key, mid_autumn.event_label) == (
        "mid_autumn",
        "中秋节",
    )


def test_content_limits_badges_and_greeting_and_accepts_plain_birthday_data() -> None:
    content = resolve_checkin_content(
        make_record(
            total_days_after=100,
            streak_days_after=14,
            boost_active=True,
            boost_multiplier=2.0,
        ),
        make_profile(total_days=100, streak_days=14),
        birthday_label="生日",
    )

    assert content.event_key == "birthday"
    assert len(content.badges) <= 2
    assert len(content.greeting) <= 44
    assert content.context.username == "Alice"
    assert content.context.user_id_hint != "10001"
    assert "10001" not in content.context.to_plain_text()
