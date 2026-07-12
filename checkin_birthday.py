from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Mapping


def parse_month_day(value: object) -> tuple[int, int] | None:
    if isinstance(value, Mapping):
        month = value.get("month") or value.get("birthday_month")
        day = value.get("day") or value.get("birthday_day")
        parsed = _valid_pair(month, day)
        if parsed:
            return parsed
    numeric_text = ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric_text = str(int(value))
        if len(numeric_text) == 8:
            try:
                parsed_date = date.fromisoformat(
                    f"{numeric_text[:4]}-{numeric_text[4:6]}-{numeric_text[6:]}"
                )
                return parsed_date.month, parsed_date.day
            except ValueError:
                pass
        if len(numeric_text) == 4:
            parsed = _valid_pair(numeric_text[:2], numeric_text[2:])
            if parsed:
                return parsed
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and len(numeric_text) in {9, 10}
    ):
        try:
            converted = datetime.fromtimestamp(int(value))
            return converted.month, converted.day
        except (OSError, OverflowError, ValueError):
            return None
    text = str(value or "").strip()
    matches = re.findall(r"\d+", text)
    if len(matches) >= 3:
        return _valid_pair(matches[-2], matches[-1])
    if len(matches) == 2:
        return _valid_pair(matches[0], matches[1])
    if len(matches) == 1 and len(matches[0]) in {4, 8}:
        digits = matches[0]
        return _valid_pair(digits[-4:-2], digits[-2:])
    return None


def parse_qq_birthday(payload: object) -> tuple[int, int] | None:
    if not isinstance(payload, Mapping):
        return None
    pair = _valid_pair(payload.get("birthday_month"), payload.get("birthday_day"))
    if pair:
        return pair
    for key in ("birthday", "birth", "birthday_info", "profile"):
        pair = parse_month_day(payload.get(key))
        if pair:
            return pair
    for key in ("data", "baseInfo", "base_info", "simpleInfo", "detail"):
        nested = payload.get(key)
        pair = parse_qq_birthday(nested)
        if pair:
            return pair
    return None


def birthday_matches(date_key: str, month: int, day: int) -> bool:
    current = date.fromisoformat(date_key)
    return current.month == month and current.day == day


def _valid_pair(month: object, day: object) -> tuple[int, int] | None:
    try:
        parsed_month, parsed_day = int(month), int(day)
        date(2000, parsed_month, parsed_day)
        return parsed_month, parsed_day
    except (TypeError, ValueError):
        return None
