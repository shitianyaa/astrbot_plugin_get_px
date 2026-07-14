from __future__ import annotations

from contextlib import closing
import sqlite3

from .themes import CHECKIN_THEMES


CHECKIN_DB_SCHEMA_VERSION = 1


class SchemaMixin:
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if schema_version not in (0, CHECKIN_DB_SCHEMA_VERSION):
                raise RuntimeError(
                    f"unsupported check-in database schema: {schema_version}"
                )
            self._create_checkin_schema(conn)
            self._sync_builtin_themes(conn)
            conn.execute(f"PRAGMA user_version = {CHECKIN_DB_SCHEMA_VERSION}")
            conn.commit()

    @staticmethod
    def _create_checkin_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkin_themes (
                theme_id TEXT PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                price INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                sort_order INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkin_users (
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
                birthday_month INTEGER NOT NULL DEFAULT 0,
                birthday_day INTEGER NOT NULL DEFAULT 0,
                birthday_source TEXT NOT NULL DEFAULT '',
                qq_birthday_checked INTEGER NOT NULL DEFAULT 0,
                selected_title_id TEXT NOT NULL DEFAULT '',
                current_theme_id TEXT NOT NULL DEFAULT 'default',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (current_theme_id) REFERENCES checkin_themes(theme_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS checkin_user_themes (
                user_id TEXT NOT NULL,
                theme_id TEXT NOT NULL,
                price_paid INTEGER NOT NULL DEFAULT 0,
                acquired_at TEXT NOT NULL,
                PRIMARY KEY (user_id, theme_id),
                FOREIGN KEY (user_id) REFERENCES checkin_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (theme_id) REFERENCES checkin_themes(theme_id)
            )
            """
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
                template_version TEXT NOT NULL DEFAULT 'default:1',
                theme_id TEXT NOT NULL DEFAULT 'default',
                background_mode TEXT NOT NULL DEFAULT '',
                background_source TEXT NOT NULL DEFAULT '',
                background_illust_id TEXT NOT NULL DEFAULT '',
                background_title TEXT NOT NULL DEFAULT '',
                background_author TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date_key, user_id),
                FOREIGN KEY (user_id) REFERENCES checkin_users(user_id) ON DELETE CASCADE,
                FOREIGN KEY (theme_id) REFERENCES checkin_themes(theme_id)
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
                PRIMARY KEY (user_id, achievement_id),
                FOREIGN KEY (user_id) REFERENCES checkin_users(user_id) ON DELETE CASCADE
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
                PRIMARY KEY (date_key, group_id, user_id),
                FOREIGN KEY (user_id) REFERENCES checkin_users(user_id) ON DELETE CASCADE
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

    @staticmethod
    def _sync_builtin_themes(conn: sqlite3.Connection) -> None:
        rows = [
            (
                theme.theme_id,
                theme.code,
                theme.name,
                theme.description,
                theme.version,
                theme.price,
                int(theme.enabled),
                sort_order,
            )
            for sort_order, theme in enumerate(CHECKIN_THEMES.values())
        ]
        conn.executemany(
            """
            INSERT INTO checkin_themes
                (theme_id, code, name, description, version, price, enabled, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(theme_id) DO UPDATE SET
                code = excluded.code,
                name = excluded.name,
                description = excluded.description,
                version = excluded.version,
                price = excluded.price,
                enabled = excluded.enabled,
                sort_order = excluded.sort_order
            """,
            rows,
        )
