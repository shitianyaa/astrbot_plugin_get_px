from __future__ import annotations

import tempfile
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin import CheckinStore
from astrbot_plugin_get_px.main import GetPxPlugin


class FakeEvent:
    def __init__(self, payload=None, *, platform="aiocqhttp"):
        self.bot = SimpleNamespace(call_action=AsyncMock(return_value=payload or {}))
        self._platform = platform

    def get_sender_id(self):
        return "10001"

    def get_platform_name(self):
        return self._platform


def make_plugin(data_dir: str) -> GetPxPlugin:
    plugin = object.__new__(GetPxPlugin)
    plugin.checkin_store = CheckinStore(data_dir)
    return plugin


@pytest.mark.asyncio
async def test_birthday_command_manual_clear_and_direct_fetch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent({"birthday_year": 2000, "birthday_month": 7, "birthday_day": 11})
        assert "07-11" in await plugin._handle_checkin_birthday(event, "设置", "07-11")
        assert "手动" in await plugin._handle_checkin_birthday(event, "", "")
        assert "已清除" in await plugin._handle_checkin_birthday(event, "清除", "")
        assert "07-11" in await plugin._handle_checkin_birthday(event, "", "")


@pytest.mark.asyncio
async def test_birthday_direct_lookup_reports_private_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent(
            {"birthday_year": 0, "birthday_month": 0, "birthday_day": 0}
        )

        assert await plugin._handle_checkin_birthday(event, "", "") == "用户未公开生日"


@pytest.mark.asyncio
async def test_automatic_birthday_is_attempted_only_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent({})
        first = await plugin._ensure_checkin_birthday(event, "10001")
        second = await plugin._ensure_checkin_birthday(event, "10001")
        assert first.qq_birthday_checked and second.qq_birthday_checked
        assert event.bot.call_action.await_count == 1
        assert event.bot.call_action.await_args.kwargs["no_cache"] is True


@pytest.mark.asyncio
async def test_non_onebot_birthday_marks_attempt_without_platform_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent(platform="webchat")
        preference = await plugin._ensure_checkin_birthday(event, "10001")
        assert preference.qq_birthday_checked
        event.bot.call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_admin_and_title_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent()
        added = await plugin._handle_checkin_event_admin(
            event, "添加年度", "07-11", "相遇纪念日", ""
        )
        assert "已添加事件" in added
        assert "相遇纪念日" in await plugin._handle_checkin_event_admin(
            event, "列表", "", "", ""
        )
        profile = await plugin.checkin_store.get_profile("10001")
        await plugin.checkin_store.unlock_achievements(
            profile.__class__(**{**profile.__dict__, "total_days": 1})
        )
        assert "初见旅人" in await plugin._handle_checkin_titles(event)
        assert "已佩戴" in await plugin._handle_select_checkin_title(event, "初见旅人")
