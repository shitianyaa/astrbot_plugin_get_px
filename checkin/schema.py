from __future__ import annotations

from contextlib import closing
import sqlite3


class SchemaMixin:
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_profiles (
                    user_id TEXT PRIMARY KEY,
                    coins INTEGER NOT NULL DEFAULT 0,
                    affection REAL NOT NULL DEFAULT 0,
                    total_days INTEGER NOT NULL DEFAULT 0,
                    streak_days INTEGER NOT NULL DEFAULT 0,
                    last_checkin_date TEXT NOT NULL DEFAULT '',
                    boost_start_date TEXT NOT NULL DEFAULT '',
                    boost_until_date TEXT NOT NULL DEFAULT '',
                    repeat_penalty_date TEXT NOT NULL DEFAULT '',
                    repeat_penalty_total REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(checkin_profiles)")
            }
            for name, ddl in {
                "boost_start_date": "TEXT NOT NULL DEFAULT ''",
                "boost_until_date": "TEXT NOT NULL DEFAULT ''",
                "repeat_penalty_date": "TEXT NOT NULL DEFAULT ''",
                "repeat_penalty_total": "REAL NOT NULL DEFAULT 0",
            }.items():
                if name not in columns:
                    conn.execute(
                        f"ALTER TABLE checkin_profiles ADD COLUMN {name} {ddl}"
                    )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_records (
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
            record_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(checkin_records)")
            }
            for name, ddl in {
                "event_key": "TEXT NOT NULL DEFAULT ''",
                "event_label": "TEXT NOT NULL DEFAULT ''",
                "greeting": "TEXT NOT NULL DEFAULT ''",
                "greeting_source": "TEXT NOT NULL DEFAULT 'local'",
                "greeting_attribution": "TEXT NOT NULL DEFAULT ''",
                "secondary_note": "TEXT NOT NULL DEFAULT ''",
                "template_version": "TEXT NOT NULL DEFAULT 'v2'",
                "theme_id": "TEXT NOT NULL DEFAULT 'default'",
                "background_mode": "TEXT NOT NULL DEFAULT ''",
                "background_source": "TEXT NOT NULL DEFAULT ''",
                "background_illust_id": "TEXT NOT NULL DEFAULT ''",
                "background_title": "TEXT NOT NULL DEFAULT ''",
                "background_author": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in record_columns:
                    conn.execute(f"ALTER TABLE checkin_records ADD COLUMN {name} {ddl}")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_user_preferences (
                    user_id TEXT PRIMARY KEY,
                    birthday_month INTEGER NOT NULL DEFAULT 0,
                    birthday_day INTEGER NOT NULL DEFAULT 0,
                    birthday_source TEXT NOT NULL DEFAULT '',
                    qq_birthday_checked INTEGER NOT NULL DEFAULT 0,
                    selected_title_id TEXT NOT NULL DEFAULT '',
                    selected_theme_id TEXT NOT NULL DEFAULT 'default',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            preference_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(checkin_user_preferences)")
            }
            if "selected_theme_id" not in preference_columns:
                conn.execute(
                    "ALTER TABLE checkin_user_preferences "
                    "ADD COLUMN selected_theme_id TEXT NOT NULL DEFAULT 'default'"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_theme_purchases (
                    user_id TEXT NOT NULL,
                    theme_id TEXT NOT NULL,
                    cost INTEGER NOT NULL DEFAULT 0,
                    purchased_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, theme_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_global_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    date_value TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_type, date_value)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_achievements (
                    user_id TEXT NOT NULL,
                    achievement_id TEXT NOT NULL,
                    unlocked_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, achievement_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS checkin_group_presence (
                    date_key TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    group_name TEXT NOT NULL DEFAULT '',
                    platform TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (date_key, group_id, user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_checkin_group_presence_lookup
                ON checkin_group_presence (group_id, date_key, first_seen_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_checkin_records_member_updated
                ON checkin_records (user_id, updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_checkin_group_presence_member_seen
                ON checkin_group_presence (user_id, last_seen_at)
                """
            )
            conn.commit()
