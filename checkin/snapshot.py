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
from .themes import CHECKIN_THEMES


def dump_checkin_snapshot_json(snapshot: dict[str, Any]) -> str:
    normalized = validate_checkin_snapshot(snapshot)
    return json.dumps(normalized, ensure_ascii=False, indent=2)


def load_checkin_snapshot_json(raw: str | bytes) -> dict[str, Any]:
    text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else str(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("签到备份文件不是合法的 JSON") from exc
    return validate_checkin_snapshot(data)


def validate_checkin_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("签到备份数据必须是对象")
    schema_version = snapshot.get("schema_version")
    if schema_version != CHECKIN_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的签到备份版本: {schema_version!r}，"
            f"当前版本为 {CHECKIN_SNAPSHOT_SCHEMA_VERSION}"
        )
    if str(snapshot.get("plugin_name") or "").strip() != CHECKIN_SNAPSHOT_PLUGIN_NAME:
        raise ValueError("签到备份文件不属于当前插件")
    if str(snapshot.get("scope") or "").strip() != CHECKIN_SNAPSHOT_SCOPE:
        raise ValueError("签到备份文件作用域不正确")
    exported_at = str(snapshot.get("exported_at") or "").strip()
    if not exported_at:
        raise ValueError("签到备份缺少 exported_at")

    collections = {
        "users": snapshot.get("users"),
        "records": snapshot.get("records"),
        "global_events": snapshot.get("global_events"),
        "achievements": snapshot.get("achievements"),
        "user_themes": snapshot.get("user_themes"),
        "group_presence": snapshot.get("group_presence"),
    }
    for key, value in collections.items():
        if not isinstance(value, list):
            raise ValueError(f"签到备份 {key} 必须是数组")

    users = [_normalize_user_row(row, i) for i, row in enumerate(collections["users"])]
    records = [
        _normalize_record_row(row, i) for i, row in enumerate(collections["records"])
    ]
    events = [
        _normalize_global_event_row(row, i)
        for i, row in enumerate(collections["global_events"])
    ]
    achievements = [
        _normalize_achievement_row(row, i)
        for i, row in enumerate(collections["achievements"])
    ]
    user_themes = [
        _normalize_user_theme_row(row, i)
        for i, row in enumerate(collections["user_themes"])
    ]
    group_presence = [
        _normalize_group_presence_row(row, i)
        for i, row in enumerate(collections["group_presence"])
    ]

    user_ids: set[str] = set()
    for index, user in enumerate(users):
        user_id = user["user_id"]
        if user_id in user_ids:
            raise ValueError(f"users[{index}].user_id duplicate: {user_id}")
        user_ids.add(user_id)

    owned_keys: set[tuple[str, str]] = set()
    for index, owned in enumerate(user_themes):
        key = (owned["user_id"], owned["theme_id"])
        if key in owned_keys:
            raise ValueError(f"user_themes[{index}] duplicate")
        if owned["user_id"] not in user_ids:
            raise ValueError(f"user_themes[{index}].user_id has no matching user")
        owned_keys.add(key)

    for index, user in enumerate(users):
        if (user["user_id"], user["current_theme_id"]) not in owned_keys:
            raise ValueError(f"users[{index}].current_theme_id 未拥有")

    record_keys: set[tuple[str, str]] = set()
    for index, record in enumerate(records):
        key = (record["date_key"], record["user_id"])
        if key in record_keys:
            raise ValueError(f"records[{index}] duplicate")
        if record["user_id"] not in user_ids:
            raise ValueError(f"records[{index}].user_id has no matching user")
        record_keys.add(key)

    achievement_keys: set[tuple[str, str]] = set()
    for index, achievement in enumerate(achievements):
        key = (achievement["user_id"], achievement["achievement_id"])
        if key in achievement_keys:
            raise ValueError(f"achievements[{index}] duplicate")
        if achievement["user_id"] not in user_ids:
            raise ValueError(f"achievements[{index}].user_id has no matching user")
        achievement_keys.add(key)
    for index, user in enumerate(users):
        selected = user["selected_title_id"]
        if selected and (user["user_id"], selected) not in achievement_keys:
            raise ValueError(f"users[{index}].selected_title_id 未解锁")

    event_ids: set[int] = set()
    event_dates: set[tuple[str, str]] = set()
    for index, event in enumerate(events):
        event_key = (event["event_type"], event["date_value"])
        if event["event_id"] in event_ids or event_key in event_dates:
            raise ValueError(f"global_events[{index}] duplicate")
        event_ids.add(event["event_id"])
        event_dates.add(event_key)

    presence_keys: set[tuple[str, str, str]] = set()
    for index, presence in enumerate(group_presence):
        key = (presence["date_key"], presence["group_id"], presence["user_id"])
        if key in presence_keys:
            raise ValueError(f"group_presence[{index}] duplicate")
        if presence["user_id"] not in user_ids:
            raise ValueError(f"group_presence[{index}].user_id has no matching user")
        presence_keys.add(key)

    return {
        "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
        "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
        "scope": CHECKIN_SNAPSHOT_SCOPE,
        "exported_at": exported_at,
        "users": users,
        "records": records,
        "global_events": events,
        "achievements": achievements,
        "user_themes": user_themes,
        "group_presence": group_presence,
    }


def _normalize_user_row(row: Any, index: int) -> dict[str, Any]:
    location = f"users[{index}]"
    if not isinstance(row, dict):
        raise ValueError(f"{location} 必须是对象")
    month = _require_int(row, "birthday_month", location)
    day = _require_int(row, "birthday_day", location)
    source = _optional_text(row, "birthday_source", "")
    if (month, day) != (0, 0):
        validate_month_day(month, day)
        if source not in {"manual", "qq"}:
            raise ValueError(f"{location}.birthday_source 无效")
    selected_title = _optional_text(row, "selected_title_id", "")
    if selected_title and selected_title not in ACHIEVEMENTS:
        raise ValueError(f"{location}.selected_title_id 无效")
    return {
        "user_id": _require_text(row, "user_id", location),
        "coins": _require_int(row, "coins", location),
        "affection": _require_float(row, "affection", location),
        "total_days": _require_int(row, "total_days", location),
        "streak_days": _require_int(row, "streak_days", location),
        "last_checkin_date": _require_text(
            row, "last_checkin_date", location, allow_blank=True
        ),
        "boost_start_date": _require_text(
            row, "boost_start_date", location, allow_blank=True
        ),
        "boost_until_date": _require_text(
            row, "boost_until_date", location, allow_blank=True
        ),
        "repeat_penalty_date": _require_text(
            row, "repeat_penalty_date", location, allow_blank=True
        ),
        "repeat_penalty_total": _require_float(row, "repeat_penalty_total", location),
        "birthday_month": month,
        "birthday_day": day,
        "birthday_source": source,
        "qq_birthday_checked": _require_boolish_int(
            row, "qq_birthday_checked", location
        ),
        "selected_title_id": selected_title,
        "current_theme_id": _validated_theme_id(
            _require_text(row, "current_theme_id", location),
            f"{location}.current_theme_id",
        ),
        "created_at": _require_text(row, "created_at", location),
        "updated_at": _require_text(row, "updated_at", location),
    }


def _normalize_record_row(row: Any, index: int) -> dict[str, Any]:
    location = f"records[{index}]"
    if not isinstance(row, dict):
        raise ValueError(f"{location} 必须是对象")
    return {
        "date_key": _require_text(row, "date_key", location),
        "user_id": _require_text(row, "user_id", location),
        "username": _require_text(row, "username", location),
        "bot_name": _require_text(row, "bot_name", location),
        "base_coins": _require_int(row, "base_coins", location),
        "bonus_coins": _require_int(row, "bonus_coins", location),
        "coins_reward": _require_int(row, "coins_reward", location),
        "base_affection": _require_float(row, "base_affection", location),
        "bonus_affection": _require_float(row, "bonus_affection", location),
        "affection_reward": _require_float(row, "affection_reward", location),
        "boost_active": _require_boolish_int(row, "boost_active", location),
        "boost_multiplier": _require_float(row, "boost_multiplier", location),
        "total_coins_after": _require_int(row, "total_coins_after", location),
        "total_affection_after": _require_float(row, "total_affection_after", location),
        "total_days_after": _require_int(row, "total_days_after", location),
        "streak_days_after": _require_int(row, "streak_days_after", location),
        "note": _require_text(row, "note", location),
        "event_key": _optional_text(row, "event_key", ""),
        "event_label": _optional_text(row, "event_label", ""),
        "greeting": _optional_text(row, "greeting", ""),
        "greeting_source": validate_greeting_source(
            _optional_text(row, "greeting_source", "local"),
            f"{location}.greeting_source",
        ),
        "greeting_attribution": _optional_text(row, "greeting_attribution", ""),
        "secondary_note": _optional_text(row, "secondary_note", ""),
        "template_version": _require_text(row, "template_version", location),
        "theme_id": _validated_theme_id(
            _require_text(row, "theme_id", location), f"{location}.theme_id"
        ),
        "background_mode": _require_text(
            row, "background_mode", location, allow_blank=True
        ),
        "background_source": _require_text(
            row, "background_source", location, allow_blank=True
        ),
        "background_illust_id": _require_text(
            row, "background_illust_id", location, allow_blank=True
        ),
        "background_title": _require_text(
            row, "background_title", location, allow_blank=True
        ),
        "background_author": _require_text(
            row, "background_author", location, allow_blank=True
        ),
        "created_at": _require_text(row, "created_at", location),
        "updated_at": _require_text(row, "updated_at", location),
    }


def _normalize_user_theme_row(row: Any, index: int) -> dict[str, Any]:
    location = f"user_themes[{index}]"
    if not isinstance(row, dict):
        raise ValueError(f"{location} 必须是对象")
    price_paid = _require_int(row, "price_paid", location)
    if price_paid < 0:
        raise ValueError(f"{location}.price_paid 不能为负数")
    return {
        "user_id": _require_text(row, "user_id", location),
        "theme_id": _validated_theme_id(
            _require_text(row, "theme_id", location), f"{location}.theme_id"
        ),
        "price_paid": price_paid,
        "acquired_at": _require_text(row, "acquired_at", location),
    }


def _normalize_group_presence_row(row: Any, index: int) -> dict[str, Any]:
    location = f"group_presence[{index}]"
    if not isinstance(row, dict):
        raise ValueError(f"{location} 必须是对象")
    date_key = _require_text(row, "date_key", location)
    try:
        date.fromisoformat(date_key)
    except ValueError as exc:
        raise ValueError(f"{location}.date_key 无效") from exc
    return {
        "date_key": date_key,
        "group_id": _require_text(row, "group_id", location),
        "group_name": _optional_text(row, "group_name", ""),
        "platform": _optional_text(row, "platform", ""),
        "user_id": _require_text(row, "user_id", location),
        "username": _require_text(row, "username", location),
        "first_seen_at": _require_text(row, "first_seen_at", location),
        "last_seen_at": _require_text(row, "last_seen_at", location),
    }


def _normalize_global_event_row(row: Any, index: int) -> dict[str, Any]:
    location = f"global_events[{index}]"
    if not isinstance(row, dict):
        raise ValueError(f"{location} 必须是对象")
    event_type, date_value = validate_global_event_date(
        _require_text(row, "event_type", location),
        _require_text(row, "date_value", location),
    )
    return {
        "event_id": _require_int(row, "event_id", location),
        "event_type": event_type,
        "date_value": date_value,
        "name": clean_event_name(_require_text(row, "name", location)),
        "created_by": _optional_text(row, "created_by", ""),
        "created_at": _require_text(row, "created_at", location),
        "updated_at": _require_text(row, "updated_at", location),
    }


def _normalize_achievement_row(row: Any, index: int) -> dict[str, Any]:
    location = f"achievements[{index}]"
    if not isinstance(row, dict):
        raise ValueError(f"{location} 必须是对象")
    achievement_id = _require_text(row, "achievement_id", location)
    if achievement_id not in ACHIEVEMENTS:
        raise ValueError(f"{location}.achievement_id 无效")
    return {
        "user_id": _require_text(row, "user_id", location),
        "achievement_id": achievement_id,
        "unlocked_at": _require_text(row, "unlocked_at", location),
    }


def _validated_theme_id(value: str, location: str) -> str:
    theme_id = str(value or "")
    if theme_id not in CHECKIN_THEMES:
        raise ValueError(f"{location} 无效")
    return theme_id


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
    return default if value is None else str(value)


def _require_int(row: dict[str, Any], key: str, location: str) -> int:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
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
        return int(value)
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{location}.{key} 必须是布尔值或 0/1")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是布尔值或 0/1") from exc
    if parsed not in (0, 1):
        raise ValueError(f"{location}.{key} 只能是 0 或 1")
    return parsed
