from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp


HOLIDAY_REFRESH_DAYS = 180
HOLIDAY_RETRY_HOURS = 24
HOLIDAY_SOURCE = (
    "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{year}.json"
)


@dataclass(frozen=True)
class OnlineHoliday:
    name: str
    is_off_day: bool


class HolidayCalendar:
    def __init__(self, data_dir: Path | str, *, plugin_version: str) -> None:
        self.path = Path(data_dir) / "holiday_calendar.json"
        self.plugin_version = plugin_version
        self._state: dict[str, Any] = self._load()

    def lookup(self, date_key: str) -> OnlineHoliday | None:
        item = self._state.get("days", {}).get(date_key)
        if not isinstance(item, dict) or not item.get("name"):
            return None
        return OnlineHoliday(
            name=str(item["name"]),
            is_off_day=bool(item.get("is_off_day", False)),
        )

    def should_refresh(self, *, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self._within_retry_window(now):
            return False
        if self._state.get("plugin_version") != self.plugin_version:
            return True
        years = self._state.get("years", [])
        if not isinstance(years, list) or now.year not in years:
            return True
        last_success = self._parse_time(self._state.get("last_success_at"))
        if last_success is None:
            return True
        return now - last_success >= timedelta(days=HOLIDAY_REFRESH_DAYS)

    async def refresh_if_due(self) -> bool:
        now = datetime.now(timezone.utc)
        if not self.should_refresh(now=now):
            return False

        self._state["plugin_version"] = self.plugin_version
        self._state["last_attempt_at"] = now.isoformat()
        self._save()

        years = (date.today().year, date.today().year + 1)
        try:
            cached_days = self._state.get("days", {})
            days: dict[str, dict[str, Any]] = {
                key: value
                for key, value in (
                    cached_days.items() if isinstance(cached_days, dict) else ()
                )
                if isinstance(key, str)
                and isinstance(value, dict)
                and any(key.startswith(f"{year}-") for year in years)
            }
            successful_years: list[int] = []
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for index, year in enumerate(years):
                    try:
                        async with session.get(
                            HOLIDAY_SOURCE.format(year=year)
                        ) as response:
                            response.raise_for_status()
                            payload = await response.json(content_type=None)
                        days.update(self._parse_year(payload, year))
                        successful_years.append(year)
                    except (aiohttp.ClientError, ValueError, TypeError):
                        if index == 0:
                            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
            return False

        self._state.update(
            {
                "schema_version": 1,
                "plugin_version": self.plugin_version,
                "last_success_at": datetime.now(timezone.utc).isoformat(),
                "years": successful_years,
                "source": HOLIDAY_SOURCE,
                "days": days,
            }
        )
        self._save()
        return True

    def _within_retry_window(self, now: datetime) -> bool:
        last_attempt = self._parse_time(self._state.get("last_attempt_at"))
        last_success = self._parse_time(self._state.get("last_success_at"))
        failed_attempt = bool(
            last_attempt and (last_success is None or last_attempt > last_success)
        )
        return bool(
            failed_attempt
            and last_attempt
            and now - last_attempt < timedelta(hours=HOLIDAY_RETRY_HOURS)
        )

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"schema_version": 1, "days": {}}
        if not isinstance(payload, dict) or not isinstance(
            payload.get("days", {}), dict
        ):
            return {"schema_version": 1, "days": {}}
        return payload

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    @staticmethod
    def _parse_year(payload: object, year: int) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("days"), list):
            raise ValueError(f"invalid holiday data for {year}")
        result: dict[str, dict[str, Any]] = {}
        for item in payload["days"]:
            if not isinstance(item, dict):
                continue
            date_key = str(item.get("date", ""))
            name = str(item.get("name", "")).strip()
            if not date_key.startswith(f"{year}-") or not name:
                continue
            result[date_key] = {
                "name": name,
                "is_off_day": bool(item.get("isOffDay", False)),
            }
        if not result:
            raise ValueError(f"empty holiday data for {year}")
        return result
