from __future__ import annotations

from contextlib import closing
import sqlite3
from pathlib import Path

import pytest

from checkin import CheckinStore


def _replace_with_legacy_records_table(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_checkin_records_member_updated")
    conn.execute("DROP TABLE checkin_records")
    conn.execute(
        """
        CREATE TABLE checkin_records (
            date_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            bot_name TEXT NOT NULL DEFAULT '',
            base_coins INTEGER NOT NULL DEFAULT 0,
            bonus_coins INTEGER NOT NULL DEFAULT 0,
            coins_reward INTEGER NOT NULL DEFAULT 0,
            base_affection REAL NOT NULL DEFAULT 0,
            bonus_affection REAL NOT NULL DEFAULT 0,
            affection_reward REAL NOT NULL DEFAULT 0,
            boost_active INTEGER NOT NULL DEFAULT 0,
            boost_multiplier REAL NOT NULL DEFAULT 1,
            total_coins_after INTEGER NOT NULL DEFAULT 0,
            total_affection_after REAL NOT NULL DEFAULT 0,
            total_days_after INTEGER NOT NULL DEFAULT 0,
            streak_days_after INTEGER NOT NULL DEFAULT 0,
            note TEXT NOT NULL DEFAULT '',
            event_key TEXT NOT NULL DEFAULT '',
            event_label TEXT NOT NULL DEFAULT '',
            greeting TEXT NOT NULL DEFAULT '',
            greeting_source TEXT NOT NULL DEFAULT 'local',
            greeting_attribution TEXT NOT NULL DEFAULT '',
            secondary_note TEXT NOT NULL DEFAULT '',
            template_version TEXT NOT NULL DEFAULT 'v2',
            theme_id TEXT NOT NULL DEFAULT 'default',
            background_mode TEXT NOT NULL DEFAULT '',
            background_source TEXT NOT NULL DEFAULT '',
            background_illust_id TEXT NOT NULL DEFAULT '',
            background_title TEXT NOT NULL DEFAULT '',
            background_author TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (date_key, user_id)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX idx_checkin_records_member_updated
        ON checkin_records (user_id, updated_at)
        """
    )


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
        _replace_with_legacy_records_table(conn)
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
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(checkin_records)"
        ).fetchall()
        assert {str(row[2]) for row in foreign_keys} == {
            "checkin_users",
            "checkin_themes",
        }
        template_default = next(
            row[4]
            for row in conn.execute("PRAGMA table_info(checkin_records)")
            if row[1] == "template_version"
        )
        assert template_default == "'default:1'"
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


def test_unknown_legacy_purchase_theme_rolls_back_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkin.sqlite3"
    _prepare_legacy_database(database_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute(
            """
            INSERT INTO checkin_theme_purchases VALUES (
                '2519706243', '99', 999, '2026-07-02T12:00:00+08:00'
            )
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="主题购买记录包含未知主题: 99"):
        CheckinStore(tmp_path)

    with closing(sqlite3.connect(database_path)) as conn:
        profile = conn.execute(
            "SELECT coins, affection, total_days FROM checkin_users "
            "WHERE user_id = '2519706243'"
        ).fetchone()
        assert profile == (60, 1.25, 1)
        assert conn.execute(
            "SELECT cost FROM checkin_theme_purchases WHERE theme_id = '99'"
        ).fetchone() == (999,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'checkin_records'"
        ).fetchone() is not None


def test_unknown_legacy_preference_theme_rolls_back_without_data_loss(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "checkin.sqlite3"
    _prepare_legacy_database(database_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute(
            """
            UPDATE checkin_user_preferences
            SET selected_theme_id = '99'
            WHERE user_id = '2519706243'
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="偏好记录包含未知主题: 99"):
        CheckinStore(tmp_path)

    with closing(sqlite3.connect(database_path)) as conn:
        assert conn.execute(
            "SELECT selected_theme_id FROM checkin_user_preferences "
            "WHERE user_id = '2519706243'"
        ).fetchone() == ("99",)
        assert conn.execute(
            "SELECT current_theme_id FROM checkin_users "
            "WHERE user_id = '2519706243'"
        ).fetchone() == ("default",)


@pytest.mark.parametrize(
    ("legacy_table", "insert_sql", "error_message"),
    (
        (
            "checkin_user_preferences",
            """
            INSERT INTO checkin_user_preferences VALUES (
                'orphan', 1, 1, 'manual', 1, '', '01',
                '2026-01-01T00:00:00+08:00',
                '2026-01-01T00:00:00+08:00'
            )
            """,
            "偏好记录缺少对应用户: orphan",
        ),
        (
            "checkin_theme_purchases",
            """
            INSERT INTO checkin_theme_purchases VALUES (
                'orphan', '01', 100, '2026-01-01T00:00:00+08:00'
            )
            """,
            "主题购买记录缺少对应用户: orphan",
        ),
    ),
)
def test_orphaned_legacy_rows_roll_back_without_data_loss(
    tmp_path: Path,
    legacy_table: str,
    insert_sql: str,
    error_message: str,
) -> None:
    database_path = tmp_path / "checkin.sqlite3"
    _prepare_legacy_database(database_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute(insert_sql)
        conn.commit()

    with pytest.raises(RuntimeError, match=error_message):
        CheckinStore(tmp_path)

    with closing(sqlite3.connect(database_path)) as conn:
        assert conn.execute(
            f'SELECT COUNT(*) FROM "{legacy_table}" WHERE user_id = ?',
            ("orphan",),
        ).fetchone() == (1,)
        assert conn.execute(
            "SELECT coins, affection, total_days FROM checkin_users "
            "WHERE user_id = '2519706243'"
        ).fetchone() == (60, 1.25, 1)


def test_late_record_migration_failure_rolls_back_all_writes(tmp_path: Path) -> None:
    database_path = tmp_path / "checkin.sqlite3"
    _prepare_legacy_database(database_path)
    with closing(sqlite3.connect(database_path)) as conn:
        conn.execute(
            """
            UPDATE checkin_records
            SET theme_id = 'custom'
            WHERE date_key = '2026-07-13' AND user_id = '2519706243'
            """
        )
        conn.commit()

    with pytest.raises(RuntimeError, match="签到记录包含未知主题: custom"):
        CheckinStore(tmp_path)

    with closing(sqlite3.connect(database_path)) as conn:
        profile = conn.execute(
            "SELECT coins, affection, total_days FROM checkin_users "
            "WHERE user_id = '2519706243'"
        ).fetchone()
        assert profile == (60, 1.25, 1)
        assert conn.execute(
            "SELECT theme_id FROM checkin_records "
            "WHERE date_key = '2026-07-13' AND user_id = '2519706243'"
        ).fetchone() == ("custom",)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = '_checkin_records_pre_v3'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT COUNT(*) FROM checkin_profiles"
        ).fetchone() == (1,)
    assert len(
        list((tmp_path / "checkin_backups").glob("pre-v3-migration-*.sqlite3"))
    ) == 1
