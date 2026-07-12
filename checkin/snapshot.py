from __future__ import annotations

from datetime import date
import json
import math
from typing import Any
import unicodedata

from .models import (
    ACHIEVEMENTS,
    CHECKIN_SNAPSHOT_PLUGIN_NAME,
    CHECKIN_SNAPSHOT_SCHEMA_VERSION,
    CHECKIN_SNAPSHOT_SCOPE,
)


def dump_checkin_snapshot_json(snapshot: dict[str, Any]) -> str:
    normalized = validate_checkin_snapshot(snapshot)
    return json.dumps(normalized, ensure_ascii=False, indent=2)


def load_checkin_snapshot_json(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig")
    else:
        text = str(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("签到备份文件不是合法的 JSON") from exc
    return validate_checkin_snapshot(data)


def validate_checkin_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("签到备份数据必须是对象")

    schema_version = snapshot.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in (1, 2, CHECKIN_SNAPSHOT_SCHEMA_VERSION)
    ):
        raise ValueError(
            f"不支持的签到备份版本: {schema_version!r}，当前支持 1、2 和 "
            f"{CHECKIN_SNAPSHOT_SCHEMA_VERSION}"
        )

    plugin_name = str(snapshot.get("plugin_name") or "").strip()
    if plugin_name != CHECKIN_SNAPSHOT_PLUGIN_NAME:
        raise ValueError("签到备份文件不属于当前插件")

    scope = str(snapshot.get("scope") or "").strip()
    if scope != CHECKIN_SNAPSHOT_SCOPE:
        raise ValueError("签到备份文件作用域不正确")

    exported_at = str(snapshot.get("exported_at") or "").strip()
    if not exported_at:
        raise ValueError("签到备份缺少 exported_at")

    profiles = snapshot.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("签到备份 profiles 必须是数组")
    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ValueError("签到备份 records 必须是数组")

    preferences = snapshot.get("preferences") if schema_version >= 3 else []
    global_events = snapshot.get("global_events") if schema_version >= 3 else []
    achievements = snapshot.get("achievements") if schema_version >= 3 else []
    for key, value in (
        ("preferences", preferences),
        ("global_events", global_events),
        ("achievements", achievements),
    ):
        if not isinstance(value, list):
            raise ValueError(f"签到备份 {key} 必须是数组")

    normalized_profiles = [
        _normalize_profile_snapshot_row(row, index)
        for index, row in enumerate(profiles)
    ]
    normalized_records = [
        _normalize_record_snapshot_row(row, index)
        for index, row in enumerate(records)
    ]
    normalized_preferences = [
        _normalize_preference_snapshot_row(row, index)
        for index, row in enumerate(preferences)
    ]
    normalized_events = [
        _normalize_global_event_snapshot_row(row, index)
        for index, row in enumerate(global_events)
    ]
    normalized_achievements = [
        _normalize_achievement_snapshot_row(row, index)
        for index, row in enumerate(achievements)
    ]

    profile_user_ids: set[str] = set()
    for index, profile in enumerate(normalized_profiles):
        user_id = profile["user_id"]
        if user_id in profile_user_ids:
            raise ValueError(f"profiles[{index}].user_id duplicate: {user_id}")
        profile_user_ids.add(user_id)

    record_keys: set[tuple[str, str]] = set()
    for index, record in enumerate(normalized_records):
        record_key = (record["date_key"], record["user_id"])
        if record_key in record_keys:
            raise ValueError(
                f"records[{index}].date_key and user_id duplicate: "
                f"{record_key[0]}, {record_key[1]}"
            )
        record_keys.add(record_key)
        if record["user_id"] not in profile_user_ids:
            raise ValueError(
                f"records[{index}].user_id has no matching profile: "
                f"{record['user_id']}"
            )

    preference_ids: set[str] = set()
    for index, preference in enumerate(normalized_preferences):
        user_id = preference["user_id"]
        if user_id in preference_ids:
            raise ValueError(f"preferences[{index}].user_id duplicate: {user_id}")
        preference_ids.add(user_id)

    achievement_keys: set[tuple[str, str]] = set()
    for index, achievement in enumerate(normalized_achievements):
        key = (achievement["user_id"], achievement["achievement_id"])
        if key in achievement_keys:
            raise ValueError(f"achievements[{index}] duplicate")
        achievement_keys.add(key)
        if achievement["user_id"] not in profile_user_ids:
            raise ValueError(
                f"achievements[{index}].user_id has no matching profile"
            )

    event_ids: set[int] = set()
    event_dates: set[tuple[str, str]] = set()
    for index, event in enumerate(normalized_events):
        event_key = (event["event_type"], event["date_value"])
        if event["event_id"] in event_ids or event_key in event_dates:
            raise ValueError(f"global_events[{index}] duplicate")
        event_ids.add(event["event_id"])
        event_dates.add(event_key)

    for index, preference in enumerate(normalized_preferences):
        selected = preference["selected_title_id"]
        if selected and (preference["user_id"], selected) not in achievement_keys:
            raise ValueError(f"preferences[{index}].selected_title_id 未解锁")

    return {
        "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
        "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
        "scope": CHECKIN_SNAPSHOT_SCOPE,
        "exported_at": exported_at,
        "profiles": normalized_profiles,
        "records": normalized_records,
        "preferences": normalized_preferences,
        "global_events": normalized_events,
        "achievements": normalized_achievements,
    }


def _normalize_profile_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"profiles[{index}] 必须是对象")
    return {
        "user_id": _require_text(row, "user_id", f"profiles[{index}]"),
        "coins": _require_int(row, "coins", f"profiles[{index}]"),
        "affection": _require_float(row, "affection", f"profiles[{index}]"),
        "total_days": _require_int(row, "total_days", f"profiles[{index}]"),
        "streak_days": _require_int(row, "streak_days", f"profiles[{index}]"),
        "last_checkin_date": _require_text(
            row, "last_checkin_date", f"profiles[{index}]", allow_blank=True
        ),
        "boost_start_date": _require_text(
            row, "boost_start_date", f"profiles[{index}]", allow_blank=True
        ),
        "boost_until_date": _require_text(
            row, "boost_until_date", f"profiles[{index}]", allow_blank=True
        ),
        "repeat_penalty_date": _require_text(
            row, "repeat_penalty_date", f"profiles[{index}]", allow_blank=True
        ),
        "repeat_penalty_total": _require_float(
            row, "repeat_penalty_total", f"profiles[{index}]"
        ),
        "created_at": _require_text(row, "created_at", f"profiles[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"profiles[{index}]"),
    }


def _normalize_record_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"records[{index}] 必须是对象")
    return {
        "date_key": _require_text(row, "date_key", f"records[{index}]"),
        "user_id": _require_text(row, "user_id", f"records[{index}]"),
        "username": _require_text(row, "username", f"records[{index}]"),
        "bot_name": _require_text(row, "bot_name", f"records[{index}]"),
        "base_coins": _require_int(row, "base_coins", f"records[{index}]"),
        "bonus_coins": _require_int(row, "bonus_coins", f"records[{index}]"),
        "coins_reward": _require_int(row, "coins_reward", f"records[{index}]"),
        "base_affection": _require_float(
            row, "base_affection", f"records[{index}]"
        ),
        "bonus_affection": _require_float(
            row, "bonus_affection", f"records[{index}]"
        ),
        "affection_reward": _require_float(
            row, "affection_reward", f"records[{index}]"
        ),
        "boost_active": _require_boolish_int(
            row, "boost_active", f"records[{index}]"
        ),
        "boost_multiplier": _require_float(
            row, "boost_multiplier", f"records[{index}]"
        ),
        "total_coins_after": _require_int(
            row, "total_coins_after", f"records[{index}]"
        ),
        "total_affection_after": _require_float(
            row, "total_affection_after", f"records[{index}]"
        ),
        "total_days_after": _require_int(
            row, "total_days_after", f"records[{index}]"
        ),
        "streak_days_after": _require_int(
            row, "streak_days_after", f"records[{index}]"
        ),
        "note": _require_text(row, "note", f"records[{index}]"),
        "event_key": _optional_text(row, "event_key", ""),
        "event_label": _optional_text(row, "event_label", ""),
        "greeting": _optional_text(row, "greeting", ""),
        "greeting_source": validate_greeting_source(
            _optional_text(row, "greeting_source", "local"),
            f"records[{index}].greeting_source",
        ),
        "greeting_attribution": _optional_text(row, "greeting_attribution", ""),
        "secondary_note": _optional_text(row, "secondary_note", ""),
        "template_version": _optional_text(row, "template_version", "v2"),
        "background_mode": _require_text(
            row, "background_mode", f"records[{index}]", allow_blank=True
        ),
        "background_source": _require_text(
            row, "background_source", f"records[{index}]", allow_blank=True
        ),
        "background_illust_id": _require_text(
            row, "background_illust_id", f"records[{index}]", allow_blank=True
        ),
        "background_title": _require_text(
            row, "background_title", f"records[{index}]", allow_blank=True
        ),
        "background_author": _require_text(
            row, "background_author", f"records[{index}]", allow_blank=True
        ),
        "created_at": _require_text(row, "created_at", f"records[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"records[{index}]"),
    }


def _normalize_preference_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"preferences[{index}] 必须是对象")
    month = _require_int(row, "birthday_month", f"preferences[{index}]")
    day = _require_int(row, "birthday_day", f"preferences[{index}]")
    source = _optional_text(row, "birthday_source", "")
    if (month, day) != (0, 0):
        validate_month_day(month, day)
        if source not in {"manual", "qq"}:
            raise ValueError(f"preferences[{index}].birthday_source 无效")
    selected = _optional_text(row, "selected_title_id", "")
    if selected and selected not in ACHIEVEMENTS:
        raise ValueError(f"preferences[{index}].selected_title_id 无效")
    return {
        "user_id": _require_text(row, "user_id", f"preferences[{index}]"),
        "birthday_month": month,
        "birthday_day": day,
        "birthday_source": source,
        "qq_birthday_checked": _require_boolish_int(
            row, "qq_birthday_checked", f"preferences[{index}]"
        ),
        "selected_title_id": selected,
        "created_at": _require_text(row, "created_at", f"preferences[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"preferences[{index}]"),
    }


def _normalize_global_event_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"global_events[{index}] 必须是对象")
    event_type, date_value = validate_global_event_date(
        _require_text(row, "event_type", f"global_events[{index}]"),
        _require_text(row, "date_value", f"global_events[{index}]"),
    )
    return {
        "event_id": _require_int(row, "event_id", f"global_events[{index}]"),
        "event_type": event_type,
        "date_value": date_value,
        "name": clean_event_name(
            _require_text(row, "name", f"global_events[{index}]")
        ),
        "created_by": _optional_text(row, "created_by", ""),
        "created_at": _require_text(row, "created_at", f"global_events[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"global_events[{index}]"),
    }


def _normalize_achievement_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"achievements[{index}] 必须是对象")
    achievement_id = _require_text(
        row, "achievement_id", f"achievements[{index}]"
    )
    if achievement_id not in ACHIEVEMENTS:
        raise ValueError(f"achievements[{index}].achievement_id 无效")
    return {
        "user_id": _require_text(row, "user_id", f"achievements[{index}]"),
        "achievement_id": achievement_id,
        "unlocked_at": _require_text(
            row, "unlocked_at", f"achievements[{index}]"
        ),
    }


def _require_text(
    row: dict[str, Any], key: str, location: str, *, allow_blank: bool = False
) -> str:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    text = "" if value is None else str(value).strip()
    if not text and not allow_blank:
        raise ValueError(f"{location}.{key} 不能为空")
    return text


def _optional_text(row: dict[str, Any], key: str, default: str) -> str:
    value = row.get(key, default)
    if value is None:
        return default
    return str(value)


def validate_greeting_source(value: Any, location: str) -> str:
    source = "" if value is None else str(value)
    if source not in ("local", "ai", "hitokoto"):
        raise ValueError(f"{location} 仅允许 local/ai/hitokoto")
    return source


def validate_month_day(month: int, day: int) -> None:
    try:
        date(2000, int(month), int(day))
    except (TypeError, ValueError) as exc:
        raise ValueError("生日格式无效，请使用 MM-DD") from exc


def validate_global_event_date(event_type: str, value: str) -> tuple[str, str]:
    normalized_type = str(event_type or "").strip().lower()
    normalized_value = str(value or "").strip()
    if normalized_type == "annual":
        try:
            month_text, day_text = normalized_value.split("-", 1)
            validate_month_day(int(month_text), int(day_text))
            return normalized_type, f"{int(month_text):02d}-{int(day_text):02d}"
        except (ValueError, TypeError) as exc:
            raise ValueError("年度事件日期必须为 MM-DD") from exc
    if normalized_type == "once":
        try:
            return normalized_type, date.fromisoformat(normalized_value).isoformat()
        except ValueError as exc:
            raise ValueError("单次事件日期必须为 YYYY-MM-DD") from exc
    raise ValueError("事件类型仅允许 annual/once")


def clean_event_name(value: object) -> str:
    raw = str(value or "")
    cleaned = "".join(
        (" " if char.isspace() else "")
        if unicodedata.category(char) in {"Cc", "Cf"}
        else char
        for char in raw
    )
    text = " ".join(cleaned.split())
    if not text:
        raise ValueError("事件名称不能为空")
    if len(text) > 20:
        raise ValueError("事件名称最多 20 个字符")
    return text


def _require_int(row: dict[str, Any], key: str, location: str) -> int:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{location}.{key} 必须是整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{location}.{key} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是整数") from exc


def _require_float(row: dict[str, Any], key: str, location: str) -> float:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{location}.{key} 必须是数字")
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError
        return parsed
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是数字") from exc


def _require_boolish_int(row: dict[str, Any], key: str, location: str) -> int:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{location}.{key} 必须是布尔值或 0/1")
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是布尔值或 0/1") from exc
    if int_value not in (0, 1):
        raise ValueError(f"{location}.{key} 只能是 0 或 1")
    return int_value
