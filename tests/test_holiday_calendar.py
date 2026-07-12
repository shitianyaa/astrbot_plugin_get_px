from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from checkin.holiday import HolidayCalendar


def test_parse_and_lookup_online_holiday(tmp_path: Path) -> None:
    calendar = HolidayCalendar(tmp_path, plugin_version="2.6.1")
    calendar._state["days"] = HolidayCalendar._parse_year(
        {
            "days": [
                {"name": "国庆节", "date": "2026-10-01", "isOffDay": True},
                {"name": "国庆节", "date": "2026-10-10", "isOffDay": False},
            ]
        },
        2026,
    )

    holiday = calendar.lookup("2026-10-01")
    workday = calendar.lookup("2026-10-10")
    assert holiday is not None and holiday.name == "国庆节" and holiday.is_off_day
    assert workday is not None and not workday.is_off_day


def test_refresh_due_on_install_version_change_and_after_six_months(tmp_path: Path) -> None:
    now = datetime(2026, 7, 11, tzinfo=timezone.utc)
    fresh = HolidayCalendar(tmp_path, plugin_version="2.6.1")
    assert fresh.should_refresh(now=now)

    fresh._state.update(
        {
            "plugin_version": "2.6.1",
            "last_success_at": (now - timedelta(days=179)).isoformat(),
            "years": [2026],
        }
    )
    assert not fresh.should_refresh(now=now)

    fresh._state["last_success_at"] = (now - timedelta(days=180)).isoformat()
    assert fresh.should_refresh(now=now)

    fresh._state.update(
        {
            "plugin_version": "2.6.0",
            "last_attempt_at": (now - timedelta(days=2)).isoformat(),
        }
    )
    assert fresh.should_refresh(now=now)

    fresh._state.update(
        {
            "plugin_version": "2.6.0",
            "last_attempt_at": (now - timedelta(minutes=2)).isoformat(),
            "last_success_at": (now - timedelta(minutes=1)).isoformat(),
        }
    )
    assert fresh.should_refresh(now=now)


def test_cache_loads_existing_data_and_ignores_corruption(tmp_path: Path) -> None:
    path = tmp_path / "holiday_calendar.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugin_version": "2.6.1",
                "days": {
                    "2026-05-01": {"name": "劳动节", "is_off_day": True}
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = HolidayCalendar(tmp_path, plugin_version="2.6.1")
    assert loaded.lookup("2026-05-01") is not None

    path.write_text("{broken", encoding="utf-8")
    broken = HolidayCalendar(tmp_path, plugin_version="2.6.1")
    assert broken.lookup("2026-05-01") is None


def test_next_year_failure_does_not_discard_current_year(tmp_path: Path) -> None:
    class Response:
        def __init__(self, payload=None, *, fail=False):
            self.payload = payload
            self.fail = fail

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def raise_for_status(self):
            if self.fail:
                from aiohttp import ClientResponseError

                request_info = MagicMock()
                request_info.real_url = "https://example.invalid/holiday.json"
                raise ClientResponseError(request_info, (), status=404)

        async def json(self, **_kwargs):
            return self.payload

    current_year = datetime.now().year
    response = Response(
        {
            "days": [
                {
                    "name": "元旦",
                    "date": f"{current_year}-01-01",
                    "isOffDay": True,
                }
            ]
        }
    )
    session = MagicMock()
    session.get = MagicMock(side_effect=[response, Response(fail=True)])
    context = AsyncMock()
    context.__aenter__.return_value = session

    calendar = HolidayCalendar(tmp_path, plugin_version="2.6.1")
    cached_next_year_date = f"{current_year + 1}-01-02"
    calendar._state["days"] = {
        cached_next_year_date: {"name": "缓存节日", "is_off_day": True}
    }
    with patch("checkin.holiday.aiohttp.ClientSession", return_value=context):
        assert __import__("asyncio").run(calendar.refresh_if_due())
    assert calendar.lookup(f"{current_year}-01-01") is not None
    assert calendar.lookup(cached_next_year_date) is not None
    assert calendar._state["years"] == [current_year]
    future = datetime(current_year + 1, 1, 1, tzinfo=timezone.utc)
    calendar._state["last_attempt_at"] = (future - timedelta(days=2)).isoformat()
    assert calendar.should_refresh(now=future)
