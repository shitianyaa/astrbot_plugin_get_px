from __future__ import annotations

import asyncio
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from pathlib import Path

import pytest
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin import CheckinStore
from astrbot_plugin_get_px.checkin.card import CardBackground
from astrbot_plugin_get_px.checkin.shop import build_checkin_shop_items
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


def set_user_coins(plugin: GetPxPlugin, coins: int) -> None:
    with closing(sqlite3.connect(plugin.checkin_store._db_path)) as conn:
        conn.execute(
            "UPDATE checkin_users SET coins = ? WHERE user_id = ?",
            (coins, "10001"),
        )
        conn.commit()


def test_theme_cost_schema_follows_background_refresh_cost() -> None:
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    keys = list(schema)

    background_index = keys.index("checkin_background_refresh_cost")
    assert keys[background_index + 1] == "checkin_theme_cost"
    assert schema["checkin_theme_cost"] == {
        "description": "签到主题价格",
        "type": "int",
        "default": 1500,
        "slider": {"min": 0, "max": 5000, "step": 100},
        "hint": "用户购买任意非默认签到主题所需金币。设为 0 表示免费；默认“米白”主题始终免费。",
    }


def test_shop_catalog_has_stable_ids_and_categories() -> None:
    items = build_checkin_shop_items(refresh_cost=5)
    by_id = {item.item_id: item for item in items}

    assert by_id["boost:1"].category == "boost"
    assert (
        by_id["background:refresh"].render_line()
        == "签到中心 商店 刷新背景 - 5 金币"
    )
    assert (
        by_id["theme:blue"].render_line()
        == "签到中心 商店 主题 购买 01 - 浅蓝，1500 金币"
    )
    assert by_id["theme:default"].price_label == "免费"

    custom_items = build_checkin_shop_items(refresh_cost=5, theme_cost=900)
    custom_by_id = {item.item_id: item for item in custom_items}
    assert custom_by_id["theme:blue"].price == 900
    assert custom_by_id["theme:default"].price_label == "免费"


@pytest.mark.asyncio
async def test_birthday_command_manual_clear_and_direct_fetch() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent(
            {"birthday_year": 2000, "birthday_month": 7, "birthday_day": 11}
        )
        assert "07-11" in await plugin._handle_checkin_birthday(event, "设置", "07-11")
        assert "手动" in await plugin._handle_checkin_birthday(event, "", "")
        assert "07-11" in await plugin._handle_checkin_birthday(event, "查看", "")
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
async def test_non_onebot_birthday_does_not_mark_attempt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent(platform="webchat")
        preference = await plugin._ensure_checkin_birthday(event, "10001")
        assert not preference.qq_birthday_checked
        event.bot.call_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_temporary_birthday_failure_is_retried() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        event = FakeEvent()
        event.bot.call_action = AsyncMock(
            side_effect=[
                asyncio.TimeoutError,
                {"birthday_year": 2000, "birthday_month": 7, "birthday_day": 11},
            ]
        )

        first = await plugin._ensure_checkin_birthday(event, "10001")
        second = await plugin._ensure_checkin_birthday(event, "10001")

        assert not first.qq_birthday_checked
        assert second.qq_birthday_checked
        assert second.birthday_label == "07-11"
        assert event.bot.call_action.await_count == 2


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
        assert "相遇纪念日" in await plugin._handle_checkin_event_admin(
            event, "查看", "", "", ""
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
            "checkin_background_refresh_cost": 5,
            "checkin_theme_cost": 900,
        }
        event = FakeEvent()
        await plugin.checkin_store.checkin(
            user_id="10001", username="测试用户", bot_name="neko"
        )
        set_user_coins(plugin, 2000)

        shop = plugin._build_checkin_shop()
        assert "签到中心 商店 刷新背景 - 5 金币" in shop
        assert "签到中心 商店 主题 购买 01 - 浅蓝，900 金币" in shop

        purchased = await plugin._handle_buy_checkin_theme(event, "01")
        assert "购买成功" in purchased
        assert "浅蓝" in purchased
        profile = await plugin.checkin_store.get_profile("10001")
        assert profile.coins == 1100
        with closing(sqlite3.connect(plugin.checkin_store._db_path)) as conn:
            price_paid = conn.execute(
                "SELECT price_paid FROM checkin_user_themes "
                "WHERE user_id = ? AND theme_id = ?",
                ("10001", "blue"),
            ).fetchone()[0]
        assert price_paid == 900
        themes = await plugin._handle_checkin_themes(event)
        assert "[当前] 01 · 浅蓝" in themes

        switched = await plugin._handle_select_checkin_theme(event, "00")
        assert "米白" in switched
        preference = await plugin.checkin_store.get_user_preference("10001")
        assert preference.current_theme_id == "default"


@pytest.mark.asyncio
async def test_free_configured_theme_is_unlocked_without_deducting_coins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {"checkin_theme_cost": 0}
        event = FakeEvent()

        purchased = await plugin._handle_buy_checkin_theme(event, "01")

        assert "购买成功，消耗 0 金币" in purchased
        profile = await plugin.checkin_store.get_profile("10001")
        assert profile.coins == 0
        assert await plugin.checkin_store.list_owned_theme_ids("10001") == (
            "default",
            "blue",
        )
        with closing(sqlite3.connect(plugin.checkin_store._db_path)) as conn:
            price_paid = conn.execute(
                "SELECT price_paid FROM checkin_user_themes "
                "WHERE user_id = ? AND theme_id = ?",
                ("10001", "blue"),
            ).fetchone()[0]
        assert price_paid == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config",
    [
        {},
        {"checkin_theme_cost": -1},
        {"checkin_theme_cost": 5001},
        {"checkin_theme_cost": "invalid"},
        {"checkin_theme_cost": True},
        {"checkin_theme_cost": False},
        {"checkin_theme_cost": 1.5},
        {"checkin_theme_cost": 900.0},
    ],
)
async def test_invalid_theme_cost_config_falls_back_to_default(config: dict) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = config
        event = FakeEvent()
        await plugin.checkin_store.get_profile("10001")
        set_user_coins(plugin, 2000)

        assert "浅蓝，1500 金币" in plugin._build_checkin_shop()
        purchased = await plugin._handle_buy_checkin_theme(event, "01")

        assert "购买成功，消耗 1500 金币" in purchased
        profile = await plugin.checkin_store.get_profile("10001")
        assert profile.coins == 500


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
        assert preview_path.parent.name == "blue"
        assert {key: value for key, value in before.items() if key != "exported_at"} == {
            key: value for key, value in after.items() if key != "exported_at"
        }

        usage = await plugin._handle_checkin_theme_preview(event, "unknown")
        assert "用法：签到中心 商店 主题 查看 <编号>" in usage


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
@pytest.mark.parametrize(
    ("cache_enabled", "send_fails", "expected_card_exists"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, True),
    ],
)
async def test_background_refresh_cleans_only_uncached_rendered_card(
    cache_enabled: bool,
    send_fails: bool,
    expected_card_exists: bool,
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {
            "checkin_enabled": True,
            "checkin_background_mode": "pixiv_daily",
            "checkin_background_refresh_cost": 5,
        }
        event = FakeEvent()
        if send_fails:
            event.send.side_effect = RuntimeError("send failed")
        await plugin.checkin_store.checkin(
            user_id="10001", username="测试用户", bot_name="neko"
        )
        set_user_coins(plugin, 100)

        background_path = Path(tmp) / "background.png"
        PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(background_path)
        background = CardBackground(
            image_path=str(background_path),
            mode="pixiv_daily",
            source="lolicon:tag",
            illust_id="445566",
            title="Blue Sky",
            author="Someone",
            quality="medium",
        )
        card_path = Path(tmp) / "card.jpg"
        PILImage.new("RGB", (960, 540), (238, 224, 196)).save(
            card_path, format="JPEG"
        )
        cache = object() if cache_enabled else None
        plugin.checkin_cache = cache
        plugin._prepare_checkin_background = AsyncMock(return_value=background)
        plugin._refresh_checkin_hitokoto = AsyncMock(
            side_effect=lambda _event, record: record
        )
        plugin._get_checkin_user_title = AsyncMock(return_value="")
        plugin._render_checkin_card_with_fallback = AsyncMock(
            return_value=(card_path, "省流量")
        )
        plugin._record_checkin_background = AsyncMock()
        plugin._release_checkin_background_claim = AsyncMock()

        outputs = [
            item
            async for item in plugin._handle_refresh_checkin_background(
                event, _flow_locked=True
            )
        ]

        assert card_path.exists() is expected_card_exists
        assert (
            plugin._render_checkin_card_with_fallback.await_args.kwargs["cache"]
            is cache
        )
        if send_fails:
            assert outputs == [
                "更新背景失败；若金币已经扣除，重新发送“签到”可查看已保存的新背景"
            ]
            plugin._release_checkin_background_claim.assert_awaited_once()
        else:
            assert outputs == []
            plugin._record_checkin_background.assert_awaited_once()


@pytest.mark.asyncio
async def test_background_refresh_refreshes_hitokoto_greeting() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {
            "checkin_greeting_mode": "hitokoto",
            "checkin_hitokoto_categories": ["全部"],
            "checkin_hitokoto_timeout": 5.0,
        }
        plugin.checkin_greeting = SimpleNamespace(
            generate_hitokoto=AsyncMock(
                return_value=("刷新后的一言", "hitokoto", "作者 · 来源")
            )
        )
        event = FakeEvent()
        await plugin.checkin_store.checkin(
            user_id="10001", username="测试用户", bot_name="neko"
        )
        await plugin.checkin_store.update_record_content(
            user_id="10001",
            date_key=plugin.checkin_store.today_key(),
            event_key="normal",
            event_label="",
            greeting="原始一言",
            greeting_source="local",
            secondary_note="",
            template_version="default:1",
        )
        record = await plugin.checkin_store.get_today_record("10001")

        refreshed = await plugin._refresh_checkin_hitokoto(event, record)

        assert refreshed.greeting == "刷新后的一言"
        assert refreshed.greeting_source == "hitokoto"
        assert refreshed.greeting_attribution == "作者 · 来源"
        assert plugin.checkin_greeting.generate_hitokoto.await_count == 1


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
                "UPDATE checkin_users SET total_days = 30, streak_days = 7 WHERE user_id = ?",
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
        PILImage.new("RGB", (960, 540), (238, 224, 196)).save(
            preview_path, format="JPEG"
        )
        plugin._prepare_checkin_background = AsyncMock(return_value=None)
        plugin._render_checkin_card = AsyncMock(return_value=str(preview_path))
        before_snapshot = await plugin.checkin_store.export_snapshot()

        with patch(
            "astrbot_plugin_get_px.checkin.commands.logger"
        ) as mock_logger:
            outputs = [item async for item in plugin._handle_checkin_preview(event)]

        after_snapshot = await plugin.checkin_store.export_snapshot()
        assert outputs == []
        assert {
            key: value for key, value in before_snapshot.items() if key != "exported_at"
        } == {
            key: value for key, value in after_snapshot.items() if key != "exported_at"
        }
        event.send.assert_awaited_once()
        debug_message = " ".join(
            str(call) for call in mock_logger.debug.call_args_list
        )
        assert f"preview=true result={greeting_mode}" in debug_message
        render_kwargs = plugin._render_checkin_card.await_args.kwargs
        plugin._prepare_checkin_background.assert_awaited_once_with(
            event,
            render_kwargs["record"],
            claim_usage=False,
            refresh_preview=True,
            render_tier="省流量",
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


@pytest.mark.asyncio
async def test_preview_greeting_failure_uses_local_content_and_logs_safely() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = make_plugin(tmp)
        plugin.config = {
            "checkin_bot_name": "neko",
            "checkin_greeting_mode": "ai",
            "checkin_avatar_enabled": False,
        }
        plugin.holiday_calendar = None
        plugin.checkin_greeting = SimpleNamespace(
            generate_hitokoto=AsyncMock(),
            generate=AsyncMock(
                side_effect=RuntimeError("https://provider.example/?token=secret")
            ),
        )
        event = FakeEvent()
        preview_path = Path(tmp) / "preview-fallback.jpg"
        PILImage.new("RGB", (960, 540), (238, 224, 196)).save(
            preview_path, format="JPEG"
        )
        plugin._prepare_checkin_background = AsyncMock(return_value=None)
        plugin._render_checkin_card = AsyncMock(return_value=str(preview_path))

        with patch(
            "astrbot_plugin_get_px.checkin.commands.logger"
        ) as mock_logger:
            outputs = [item async for item in plugin._handle_checkin_preview(event)]

        assert outputs == []
        event.send.assert_awaited_once()
        record = plugin._render_checkin_card.await_args.kwargs["record"]
        assert record.greeting
        assert record.greeting_source == "local"
        message = str(mock_logger.warning.call_args.args[0])
        assert "preview=true" in message
        assert "error_type=RuntimeError" in message
        assert "token=secret" not in message
        debug_message = " ".join(
            str(call) for call in mock_logger.debug.call_args_list
        )
        assert "preview=true result=local" in debug_message
