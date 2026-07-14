from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from checkin.card import get_checkin_card_template
from checkin.store import CheckinStore
from checkin.themes import CHECKIN_THEMES, resolve_checkin_theme


class FrozenCheckinStore(CheckinStore):
    def __init__(self, data_dir: str, *, date_key: str):
        self.date_key = date_key
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key


def test_only_launch_themes_are_registered() -> None:
    assert tuple(CHECKIN_THEMES) == ("default", "01", "02", "03")
    assert [theme.name for theme in CHECKIN_THEMES.values()] == [
        "米白",
        "浅蓝",
        "红黑",
        "黄黑",
    ]


def test_color_names_and_legacy_names_resolve_to_the_same_theme() -> None:
    aliases = {
        "米白": "default",
        "便签画册": "default",
        "浅蓝": "01",
        "星穹乘车凭证": "01",
        "红黑": "02",
        "怪盗契约卡": "02",
        "黄黑": "03",
        "绳网控制终端": "03",
    }
    for value, theme_id in aliases.items():
        theme = resolve_checkin_theme(value)
        assert theme is not None
        assert theme.theme_id == theme_id


def test_all_registered_checkin_themes_are_self_contained() -> None:
    plugin_root = Path(__file__).resolve().parents[1]
    for theme_id in CHECKIN_THEMES:
        theme = CHECKIN_THEMES[theme_id]
        rendered = get_checkin_card_template(theme_id)
        assert "/*__CHECKIN_CARD_CSS__*/" not in rendered
        assert "__CHECKIN_CARD_FONT_DATA__" not in rendered
        assert "data:font/woff2;base64," in rendered
        assert "https://" not in rendered
        assert "http://" not in rendered
        preview = theme.preview_path(plugin_root)
        assert preview.is_file()
        assert preview.suffix.lower() == ".png"


@pytest.mark.asyncio
async def test_theme_purchase_deducts_coins_and_updates_today_record() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        result = await store.checkin(
            user_id="10001",
            username="tester",
            bot_name="neko",
        )
        assert result.record is not None
        cost = max(1, result.profile.coins // 2)

        purchase = await store.purchase_theme(
            user_id="10001",
            theme_id="01",
            cost=cost,
            template_version=CHECKIN_THEMES["01"].template_version,
        )

        assert purchase.success
        assert not purchase.already_owned
        assert purchase.profile.coins == result.profile.coins - cost
        assert "01" in await store.list_owned_theme_ids("10001")
        preference = await store.get_user_preference("10001")
        assert preference.selected_theme_id == "01"
        record = await store.get_today_record("10001")
        assert record is not None
        assert record.theme_id == "01"
        assert record.template_version == CHECKIN_THEMES["01"].template_version
        assert record.total_coins_after == purchase.profile.coins


@pytest.mark.asyncio
async def test_theme_selection_requires_ownership_but_default_is_free() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        await store.get_profile("10001")

        with pytest.raises(ValueError, match="尚未购买"):
            await store.select_theme(
                user_id="10001",
                theme_id="02",
                template_version=CHECKIN_THEMES["02"].template_version,
            )

        selected = await store.select_theme(
            user_id="10001",
            theme_id="default",
            template_version=CHECKIN_THEMES["default"].template_version,
        )
        assert selected == "default"


@pytest.mark.asyncio
async def test_background_refresh_purchase_updates_balance_and_background() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        result = await store.checkin(
            user_id="10001",
            username="tester",
            bot_name="neko",
        )
        assert result.record is not None
        cost = max(1, result.profile.coins // 2)

        purchase = await store.purchase_background_refresh(
            user_id="10001",
            cost=cost,
            mode="pixiv_daily",
            source="ranking:day",
            illust_id="445566",
            title="Blue Sky",
            author="Someone",
        )

        assert purchase.success
        assert purchase.record is not None
        assert purchase.profile.coins == result.profile.coins - cost
        assert purchase.record.total_coins_after == purchase.profile.coins
        assert purchase.record.background_illust_id == "445566"
        assert purchase.record.background_title == "Blue Sky"


@pytest.mark.asyncio
async def test_theme_purchases_survive_snapshot_round_trip() -> None:
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        source = FrozenCheckinStore(src, date_key="2026-07-13")
        result = await source.checkin(
            user_id="10001",
            username="tester",
            bot_name="neko",
        )
        await source.purchase_theme(
            user_id="10001",
            theme_id="02",
            cost=max(1, result.profile.coins // 2),
            template_version=CHECKIN_THEMES["02"].template_version,
        )

        snapshot = await source.export_snapshot()
        assert snapshot["schema_version"] == 5
        assert snapshot["theme_purchases"][0]["theme_id"] == "02"

        target = FrozenCheckinStore(dst, date_key="2026-07-13")
        await target.import_snapshot(snapshot)
        assert "02" in await target.list_owned_theme_ids("10001")
        preference = await target.get_user_preference("10001")
        assert preference.selected_theme_id == "02"
