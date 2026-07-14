from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path

import pytest

from checkin import CheckinStore


def _prepare_legacy_database(path: Path) -> None:
    CheckinStore(path.parent)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT INTO checkin_users (
                user_id, coins, affection, total_days, streak_days,
                last_checkin_date, boost_start_date, boost_until_date,
                repeat_penalty_date, repeat_penalty_total,
                birthday_month, birthday_day, birthday_source,
                qq_birthday_checked, selected_title_id, current_theme_id,
                created_at, updated_at
            ) VALUES (
                '2519706243', 60, 1.25, 1, 1,
                '2026-07-14', '', '', '', 0,
                0, 0, '', 0, '', 'default',
                '2026-07-14T20:00:00+08:00',
                '2026-07-14T20:00:00+08:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkin_user_themes
                (user_id, theme_id, price_paid, acquired_at)
            VALUES ('2519706243', 'default', 0, '2026-07-14T20:00:00+08:00')
            """
        )
        conn.execute(
            """
            CREATE TABLE checkin_profiles (
                user_id TEXT PRIMARY KEY,
                coins INTEGER NOT NULL,
                affection REAL NOT NULL,
                total_days INTEGER NOT NULL,
                streak_days INTEGER NOT NULL,
                last_checkin_date TEXT NOT NULL,
                boost_start_date TEXT NOT NULL,
                boost_until_date TEXT NOT NULL,
                repeat_penalty_date TEXT NOT NULL,
                repeat_penalty_total REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkin_profiles VALUES (
                '2519706243', 1000, 25.5, 30, 12,
                '2026-07-13', '', '', '', 0,
                '2026-06-01T12:00:00+08:00',
                '2026-07-13T12:00:00+08:00'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE checkin_user_preferences (
                user_id TEXT PRIMARY KEY,
                birthday_month INTEGER NOT NULL,
                birthday_day INTEGER NOT NULL,
                birthday_source TEXT NOT NULL,
                qq_birthday_checked INTEGER NOT NULL,
                selected_title_id TEXT NOT NULL,
                selected_theme_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkin_user_preferences VALUES (
                '2519706243', 7, 14, 'manual', 1,
                'streak_7', '01',
                '2026-06-01T12:00:00+08:00',
                '2026-07-13T12:00:00+08:00'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE checkin_theme_purchases (
                user_id TEXT NOT NULL,
                theme_id TEXT NOT NULL,
                cost INTEGER NOT NULL,
                purchased_at TEXT NOT NULL,
                PRIMARY KEY (user_id, theme_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkin_theme_purchases VALUES (
                '2519706243', '01', 1500, '2026-07-01T12:00:00+08:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkin_records (
                date_key, user_id, username, bot_name,
                theme_id, template_version, created_at, updated_at
            ) VALUES (
                '2026-07-13', '2519706243', 'Aozaki Aoko', 'bot',
                '01', 'stellar-v4',
                '2026-07-13T12:00:00+08:00',
                '2026-07-13T12:00:00+08:00'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO checkin_records (
                date_key, user_id, username, bot_name,
                theme_id, template_version, created_at, updated_at
            ) VALUES (
                '2026-07-14', '2519706243', 'Aozaki Aoko', 'bot',
                'default', 'default:1',
                '2026-07-14T20:00:00+08:00',
                '2026-07-14T20:00:00+08:00'
            )
            """
        )
        conn.commit()


@pytest.mark.asyncio
async def test_legacy_users_are_merged_back_into_existing_v3_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "checkin.sqlite3"
    _prepare_legacy_database(database_path)

    store = CheckinStore(tmp_path)

    summary = store.legacy_migration_summary
    assert summary is not None
    assert summary.users == 1
    assert summary.records == 2
    assert summary.preferences == 1
    assert summary.purchases == 1
    assert summary.backup_path.is_file()

    profile = await store.get_profile("2519706243")
    assert profile.coins == 1060
    assert profile.affection == pytest.approx(26.75)
    assert profile.total_days == 31
    assert profile.streak_days == 13
    assert profile.last_checkin_date == "2026-07-14"

    preference = await store.get_user_preference("2519706243")
    assert preference.birthday_label == "07-14"
    assert preference.selected_title_id == "streak_7"
    assert preference.current_theme_id == "blue"
    assert await store.list_owned_theme_ids("2519706243") == ("default", "blue")

    with closing(sqlite3.connect(database_path)) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert not set(summary.removed_tables) & tables
        record = conn.execute(
            "SELECT theme_id, template_version FROM checkin_records "
            "WHERE date_key = '2026-07-13' AND user_id = '2519706243'"
        ).fetchone()
        assert record == ("blue", "blue:1")
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None

    with closing(sqlite3.connect(summary.backup_path)) as backup:
        backup_tables = {
            str(row[0])
            for row in backup.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "checkin_profiles" in backup_tables
        assert "checkin_user_preferences" in backup_tables

    restarted = CheckinStore(tmp_path)
    assert restarted.legacy_migration_summary is None
    assert list((tmp_path / "checkin_backups").glob("pre-v3-migration-*.sqlite3")) == [
        summary.backup_path
    ]


def test_invalid_legacy_schema_rolls_back_without_dropping_old_table(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkin.sqlite3"
    CheckinStore(tmp_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute("CREATE TABLE checkin_profiles (user_id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO checkin_profiles VALUES ('broken-user')")
        conn.commit()

    with pytest.raises(RuntimeError, match="缺少必要字段"):
        CheckinStore(tmp_path)

    with closing(sqlite3.connect(database_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM checkin_profiles"
        ).fetchone()[0] == 1
    assert len(
        list((tmp_path / "checkin_backups").glob("pre-v3-migration-*.sqlite3"))
    ) == 1
