from __future__ import annotations

from contextlib import closing
import sqlite3
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


def _set_coins(store: CheckinStore, user_id: str, coins: int) -> None:
    with closing(sqlite3.connect(store._db_path)) as conn:
        conn.execute(
            "UPDATE checkin_users SET coins = ? WHERE user_id = ?",
            (coins, user_id),
        )
        conn.commit()


def test_only_builtin_themes_are_registered() -> None:
    assert tuple(CHECKIN_THEMES) == ("default", "blue", "red", "yellow")
    assert [theme.code for theme in CHECKIN_THEMES.values()] == [
        "00",
        "01",
        "02",
        "03",
    ]
    assert [theme.name for theme in CHECKIN_THEMES.values()] == [
        "米白",
        "浅蓝",
        "红黑",
        "黄黑",
    ]


def test_theme_ids_codes_and_names_resolve() -> None:
    values = {
        "default": "default",
        "00": "default",
        "0": "default",
        "米白": "default",
        "blue": "blue",
        "01": "blue",
        "1": "blue",
        "浅蓝": "blue",
        "red": "red",
        "02": "red",
        "红黑": "red",
        "yellow": "yellow",
        "03": "yellow",
        "黄黑": "yellow",
    }
    for value, theme_id in values.items():
        theme = resolve_checkin_theme(value)
        assert theme is not None
        assert theme.theme_id == theme_id

    for unsupported in ("默认", "蓝", "红", "黄", "light_blue"):
        assert resolve_checkin_theme(unsupported) is None


def test_all_registered_checkin_themes_are_self_contained() -> None:
    plugin_root = Path(__file__).resolve().parents[1]
    for theme in CHECKIN_THEMES.values():
        rendered = get_checkin_card_template(theme.theme_id)
        assert "/*__CHECKIN_CARD_CSS__*/" not in rendered
        assert "__CHECKIN_CARD_FONT_DATA__" not in rendered
        assert "data:font/woff2;base64," in rendered
        assert "https://" not in rendered
        assert "http://" not in rendered
        assert theme.template_dir(plugin_root).name == theme.theme_id
        preview = theme.preview_path(plugin_root)
        assert preview.is_file()
        assert preview.suffix.lower() == ".png"


def test_theme_and_user_tables_are_separate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        with closing(sqlite3.connect(store._db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "checkin_users" in tables
            assert "checkin_themes" in tables
            assert "checkin_user_themes" in tables


def test_schema_zero_rebuild_only_drops_literal_checkin_prefix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "checkin.sqlite3"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.execute("CREATE TABLE checkin_profiles (user_id TEXT PRIMARY KEY)")
            conn.execute("CREATE TABLE checkinXforeign (value TEXT)")
            conn.execute("INSERT INTO checkin_profiles VALUES ('old-user')")
            conn.execute("INSERT INTO checkinXforeign VALUES ('keep')")
            conn.commit()

        FrozenCheckinStore(tmp, date_key="2026-07-13")

        with closing(sqlite3.connect(db_path)) as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            assert "checkin_profiles" not in tables
            assert "checkin_users" in tables
            assert "checkinXforeign" in tables
            assert (
                conn.execute("SELECT value FROM checkinXforeign").fetchone()[0]
                == "keep"
            )


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
        _set_coins(store, "10001", 2000)

        purchase = await store.purchase_theme(user_id="10001", theme_id="blue")

        assert purchase.success
        assert not purchase.already_owned
        assert purchase.cost == CHECKIN_THEMES["blue"].price
        assert purchase.profile.coins == 500
        assert await store.list_owned_theme_ids("10001") == ("default", "blue")
        preference = await store.get_user_preference("10001")
        assert preference.current_theme_id == "blue"
        record = await store.get_today_record("10001")
        assert record is not None
        assert record.theme_id == "blue"
        assert record.template_version == CHECKIN_THEMES["blue"].template_version
        assert record.total_coins_after == purchase.profile.coins


@pytest.mark.asyncio
async def test_theme_selection_requires_ownership() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        await store.get_profile("10001")

        with pytest.raises(ValueError, match="尚未购买"):
            await store.select_theme(user_id="10001", theme_id="red")

        selected = await store.select_theme(user_id="10001", theme_id="default")
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
async def test_user_themes_survive_snapshot_round_trip() -> None:
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        source = FrozenCheckinStore(src, date_key="2026-07-13")
        await source.checkin(user_id="10001", username="tester", bot_name="neko")
        _set_coins(source, "10001", 2000)
        await source.purchase_theme(user_id="10001", theme_id="red")

        snapshot = await source.export_snapshot()
        assert snapshot["schema_version"] == 6
        assert {row["theme_id"] for row in snapshot["user_themes"]} == {
            "default",
            "red",
        }

        target = FrozenCheckinStore(dst, date_key="2026-07-13")
        await target.import_snapshot(snapshot)
        assert await target.list_owned_theme_ids("10001") == ("default", "red")
        preference = await target.get_user_preference("10001")
        assert preference.current_theme_id == "red"
