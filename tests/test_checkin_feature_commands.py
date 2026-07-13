from __future__ import annotations

from contextlib import closing
import sqlite3
import sys
import tempfile
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
        self.send = AsyncMock()

    def get_sender_id(self):
        return "10001"

    def get_platform_name(self):
        return self._platform

    def get_sender_name(self):
        return "测试用户"

    def chain_result(self, content):
        return content

    def plain_result(self, content):
        return content


def make_plugin(data_dir: str) -> GetPxPlugin:
    plugin = object.__new__(GetPxPlugin)
    plugin.checkin_store = CheckinStore(data_dir)
    return plugin


@pytest.mark.asyncio
async def test_birthday_command_manual_clear_and_direct_fetch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent(
            {"birthday_year": 2000, "birthday_month": 7, "birthday_day": 11}
        )
        assert "07-11" in await plugin._handle_checkin_birthday(event, "设置", "07-11")
        assert "手动" in await plugin._handle_checkin_birthday(event, "", "")
        assert "已清除" in await plugin._handle_checkin_birthday(event, "清除", "")
        assert "07-11" in await plugin._handle_checkin_birthday(event, "", "")


@pytest.mark.asyncio
async def test_birthday_direct_lookup_reports_private_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent({"birthday_year": 0, "birthday_month": 0, "birthday_day": 0})

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


@pytest.mark.asyncio
async def test_theme_shop_purchase_and_switch_commands() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {
            "checkin_enabled": True,
            "checkin_theme_price": 10,
            "checkin_background_refresh_cost": 5,
        }
        event = FakeEvent()
        await plugin.checkin_store.checkin(
            user_id="10001", username="测试用户", bot_name="neko"
        )

        shop = plugin._build_checkin_shop()
        assert "/刷新签到背景 - 5 金币" in shop
        assert "/购买主题 01 - 浅蓝，10 金币" in shop

        purchased = await plugin._handle_buy_checkin_theme(event, "01")
        assert "购买成功" in purchased
        assert "浅蓝" in purchased
        themes = await plugin._handle_checkin_themes(event)
        assert "[当前] 01 · 浅蓝" in themes

        switched = await plugin._handle_select_checkin_theme(event, "默认")
        assert "米白" in switched
        preference = await plugin.checkin_store.get_user_preference("10001")
        assert preference.selected_theme_id == "default"


@pytest.mark.asyncio
async def test_theme_preview_is_available_without_purchase_or_database_write() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent()

        before = await plugin.checkin_store.export_snapshot()
        result = await plugin._handle_checkin_theme_preview(event, "1")
        after = await plugin.checkin_store.export_snapshot()

        assert len(result) == 2
        assert result[0].text.startswith("主题预览：01 · 浅蓝")
        preview_path = Path(result[1].path)
        assert preview_path.name == "preview.png"
        assert preview_path.parent.name == "01_stellar_ticket"
        assert before == after

        usage = await plugin._handle_checkin_theme_preview(event, "unknown")
        assert "用法：/查看主题 <编号>" in usage


@pytest.mark.asyncio
async def test_background_refresh_requires_today_checkin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {
            "checkin_enabled": True,
            "checkin_background_mode": "pixiv_daily",
            "checkin_background_refresh_cost": 100,
        }
        event = FakeEvent()

        outputs = [
            item async for item in plugin._handle_refresh_checkin_background(event)
        ]
        assert outputs == ["请先完成今天的签到，再更新背景"]


@pytest.mark.asyncio
async def test_old_user_achievements_are_backfilled_and_highest_title_is_equipped() -> (
    None
):
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent()
        await plugin.checkin_store.get_profile("10001")
        with closing(sqlite3.connect(plugin.checkin_store._db_path)) as conn:
            conn.execute(
                "UPDATE checkin_profiles SET total_days = 30, streak_days = 7 WHERE user_id = ?",
                ("10001",),
            )
            conn.commit()

        achievements = await plugin._handle_checkin_achievements(event)
        titles = await plugin._handle_checkin_titles(event)
        preference = await plugin.checkin_store.get_user_preference("10001")

        assert "本次补发: 初见旅人、七日同行、月下常客" in achievements
        assert "[当前] 月下常客" in titles
        assert preference.selected_title_id == "total_30"


@pytest.mark.asyncio
@pytest.mark.parametrize("greeting_mode", ["hitokoto", "ai"])
async def test_preview_uses_real_data_and_remote_greeting_without_writes(
    greeting_mode: str,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {
            "checkin_bot_name": "neko",
            "checkin_greeting_mode": greeting_mode,
            "checkin_hitokoto_categories": ["动画", "诗词"],
            "checkin_avatar_enabled": False,
        }
        plugin.holiday_calendar = None
        plugin.checkin_greeting = SimpleNamespace(
            generate_hitokoto=AsyncMock(
                return_value=("一言测试问候", "hitokoto", "作者 · 作品")
            ),
            generate=AsyncMock(return_value=("AI 测试问候", "ai")),
        )
        event = FakeEvent()
        result = await plugin.checkin_store.checkin(
            user_id="10001", username="测试用户", bot_name="neko"
        )
        await plugin.checkin_store.unlock_achievements(result.profile)
        await plugin.checkin_store.set_birthday(
            user_id="10001", month=7, day=12, source="manual"
        )

        preview_path = Path(tmp) / "preview.jpg"
        preview_path.write_bytes(b"preview")
        plugin._prepare_checkin_background = AsyncMock(return_value=None)
        plugin._render_checkin_card = AsyncMock(return_value=str(preview_path))
        before_snapshot = await plugin.checkin_store.export_snapshot()

        outputs = [item async for item in plugin._handle_checkin_preview(event)]

        after_snapshot = await plugin.checkin_store.export_snapshot()
        assert outputs == []
        assert before_snapshot == after_snapshot
        event.send.assert_awaited_once()
        render_kwargs = plugin._render_checkin_card.await_args.kwargs
        plugin._prepare_checkin_background.assert_awaited_once_with(
            event,
            render_kwargs["record"],
            claim_usage=False,
            refresh_preview=True,
        )
        assert render_kwargs["profile"].coins == result.profile.coins
        assert render_kwargs["record"].total_days_after == result.profile.total_days
        assert render_kwargs["user_title"] == "初见旅人"
        if greeting_mode == "hitokoto":
            assert render_kwargs["record"].greeting == "一言测试问候"
            plugin.checkin_greeting.generate_hitokoto.assert_awaited_once()
            assert plugin.checkin_greeting.generate_hitokoto.await_args.kwargs[
                "categories"
            ] == ["动画", "诗词"]
            plugin.checkin_greeting.generate.assert_not_awaited()
        else:
            assert render_kwargs["record"].greeting == "AI 测试问候"
            plugin.checkin_greeting.generate.assert_awaited_once()
            plugin.checkin_greeting.generate_hitokoto.assert_not_awaited()
