from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3


LEGACY_CHECKIN_TABLES = (
    "checkin_profiles",
    "checkin_user_preferences",
    "checkin_theme_purchases",
)

_REQUIRED_PROFILE_COLUMNS = {
    "user_id",
    "coins",
    "affection",
    "total_days",
    "streak_days",
    "last_checkin_date",
    "created_at",
    "updated_at",
}


@dataclass(frozen=True)
class LegacyMigrationSummary:
    users: int
    records: int
    preferences: int
    purchases: int
    removed_tables: tuple[str, ...]
    backup_path: Path


def detect_legacy_checkin_tables(path: Path) -> tuple[str, ...]:
    path = Path(path)
    if not path.is_file():
        return ()
    try:
        with closing(sqlite3.connect(path)) as conn:
            return _legacy_tables(conn)
    except sqlite3.DatabaseError:
        return ()


def backup_legacy_checkin_database(
    conn: sqlite3.Connection, database_path: Path
) -> Path:
    backup_dir = Path(database_path).parent / "checkin_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_dir / f"pre-v3-migration-{timestamp}.sqlite3"
    with closing(sqlite3.connect(backup_path)) as backup_conn:
        conn.backup(backup_conn)
    return backup_path


def migrate_legacy_checkin_data(
    conn: sqlite3.Connection,
    *,
    legacy_tables: tuple[str, ...],
    backup_path: Path,
) -> LegacyMigrationSummary:
    if "checkin_profiles" not in legacy_tables:
        raise RuntimeError("旧版签到数据库缺少 checkin_profiles 表")

    profile_columns = _table_columns(conn, "checkin_profiles")
    missing = _REQUIRED_PROFILE_COLUMNS - profile_columns
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(f"旧版签到用户表缺少必要字段: {names}")

    users = _table_count(conn, "checkin_profiles")
    records = _table_count(conn, "checkin_records")
    preferences = (
        _table_count(conn, "checkin_user_preferences")
        if "checkin_user_preferences" in legacy_tables
        else 0
    )
    purchases = (
        _table_count(conn, "checkin_theme_purchases")
        if "checkin_theme_purchases" in legacy_tables
        else 0
    )

    optional_profile_values = {
        "boost_start_date": "''",
        "boost_until_date": "''",
        "repeat_penalty_date": "''",
        "repeat_penalty_total": "0",
    }
    profile_value = {
        name: name if name in profile_columns else fallback
        for name, fallback in optional_profile_values.items()
    }
    conn.execute(
        f"""
        INSERT INTO checkin_users (
            user_id, coins, affection, total_days, streak_days,
            last_checkin_date, boost_start_date, boost_until_date,
            repeat_penalty_date, repeat_penalty_total,
            birthday_month, birthday_day, birthday_source,
            qq_birthday_checked, selected_title_id, current_theme_id,
            created_at, updated_at
        )
        SELECT
            user_id, coins, affection, total_days, streak_days,
            last_checkin_date, {profile_value['boost_start_date']},
            {profile_value['boost_until_date']},
            {profile_value['repeat_penalty_date']},
            {profile_value['repeat_penalty_total']},
            0, 0, '', 0, '', 'default', created_at, updated_at
        FROM checkin_profiles
        WHERE true
        ON CONFLICT(user_id) DO UPDATE SET
            coins = excluded.coins + checkin_users.coins,
            affection = excluded.affection + checkin_users.affection,
            total_days = excluded.total_days + checkin_users.total_days,
            streak_days = CASE
                WHEN checkin_users.last_checkin_date > excluded.last_checkin_date
                 AND date(excluded.last_checkin_date,
                          '+' || checkin_users.streak_days || ' days')
                     = checkin_users.last_checkin_date
                THEN excluded.streak_days + checkin_users.streak_days
                WHEN checkin_users.last_checkin_date > excluded.last_checkin_date
                THEN checkin_users.streak_days
                ELSE excluded.streak_days
            END,
            last_checkin_date = max(
                excluded.last_checkin_date, checkin_users.last_checkin_date
            ),
            boost_start_date = CASE
                WHEN checkin_users.boost_until_date > excluded.boost_until_date
                THEN checkin_users.boost_start_date
                ELSE excluded.boost_start_date
            END,
            boost_until_date = max(
                excluded.boost_until_date, checkin_users.boost_until_date
            ),
            repeat_penalty_date = max(
                excluded.repeat_penalty_date,
                checkin_users.repeat_penalty_date
            ),
            repeat_penalty_total = CASE
                WHEN excluded.repeat_penalty_date
                     = checkin_users.repeat_penalty_date
                THEN excluded.repeat_penalty_total
                     + checkin_users.repeat_penalty_total
                WHEN checkin_users.repeat_penalty_date
                     > excluded.repeat_penalty_date
                THEN checkin_users.repeat_penalty_total
                ELSE excluded.repeat_penalty_total
            END,
            created_at = min(excluded.created_at, checkin_users.created_at),
            updated_at = max(excluded.updated_at, checkin_users.updated_at)
        """
    )

    if "checkin_user_preferences" in legacy_tables:
        preference_columns = _table_columns(conn, "checkin_user_preferences")
        required_preferences = {
            "user_id",
            "birthday_month",
            "birthday_day",
            "birthday_source",
            "qq_birthday_checked",
            "selected_title_id",
            "created_at",
            "updated_at",
        }
        missing_preferences = required_preferences - preference_columns
        if missing_preferences:
            names = ", ".join(sorted(missing_preferences))
            raise RuntimeError(f"旧版签到偏好表缺少必要字段: {names}")
        selected_theme_expression = (
            "preference.selected_theme_id"
            if "selected_theme_id" in preference_columns
            else "'default'"
        )
        conn.execute(
            f"""
            UPDATE checkin_users AS user
            SET birthday_month = preference.birthday_month,
                birthday_day = preference.birthday_day,
                birthday_source = preference.birthday_source,
                qq_birthday_checked = preference.qq_birthday_checked,
                selected_title_id = preference.selected_title_id,
                current_theme_id = CASE
                    WHEN user.current_theme_id <> 'default'
                    THEN user.current_theme_id
                    ELSE {_theme_id_sql(selected_theme_expression)}
                END,
                created_at = min(user.created_at, preference.created_at),
                updated_at = max(user.updated_at, preference.updated_at)
            FROM checkin_user_preferences AS preference
            WHERE preference.user_id = user.user_id
            """
        )

    conn.execute(
        """
        INSERT INTO checkin_user_themes
            (user_id, theme_id, price_paid, acquired_at)
        SELECT user_id, 'default', 0, created_at
        FROM checkin_users
        WHERE true
        ON CONFLICT(user_id, theme_id) DO NOTHING
        """
    )
    if "checkin_theme_purchases" in legacy_tables:
        purchase_columns = _table_columns(conn, "checkin_theme_purchases")
        required_purchases = {"user_id", "theme_id", "cost", "purchased_at"}
        missing_purchases = required_purchases - purchase_columns
        if missing_purchases:
            names = ", ".join(sorted(missing_purchases))
            raise RuntimeError(f"旧版签到主题购买表缺少必要字段: {names}")
        conn.execute(
            f"""
            INSERT INTO checkin_user_themes
                (user_id, theme_id, price_paid, acquired_at)
            SELECT purchase.user_id,
                   {_theme_id_sql('purchase.theme_id')},
                   purchase.cost,
                   purchase.purchased_at
            FROM checkin_theme_purchases AS purchase
            JOIN checkin_users AS user ON user.user_id = purchase.user_id
            WHERE purchase.theme_id IN ('default', '01', '02', '03')
            ON CONFLICT(user_id, theme_id) DO UPDATE SET
                price_paid = max(
                    checkin_user_themes.price_paid, excluded.price_paid
                ),
                acquired_at = min(
                    checkin_user_themes.acquired_at, excluded.acquired_at
                )
            """
        )

    conn.execute(
        """
        INSERT INTO checkin_user_themes
            (user_id, theme_id, price_paid, acquired_at)
        SELECT user_id, current_theme_id, 0, created_at
        FROM checkin_users
        WHERE true
        ON CONFLICT(user_id, theme_id) DO NOTHING
        """
    )
    conn.execute(
        """
        UPDATE checkin_records
        SET template_version = CASE
                WHEN template_version LIKE '%:%' THEN template_version
                WHEN theme_id = '01' THEN 'blue:1'
                WHEN theme_id = '02' THEN 'red:1'
                WHEN theme_id = '03' THEN 'yellow:1'
                WHEN theme_id = 'default' THEN 'default:1'
                ELSE template_version
            END,
            theme_id = CASE theme_id
                WHEN '01' THEN 'blue'
                WHEN '02' THEN 'red'
                WHEN '03' THEN 'yellow'
                ELSE theme_id
            END
        WHERE theme_id IN ('default', '01', '02', '03')
        """
    )

    migrated_users = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM checkin_profiles AS legacy
            JOIN checkin_users AS current USING (user_id)
            """
        ).fetchone()[0]
    )
    if migrated_users != users:
        raise RuntimeError(
            f"旧版签到用户迁移数量不一致: expected={users}, actual={migrated_users}"
        )
    unowned_theme = conn.execute(
        """
        SELECT 1
        FROM checkin_users AS user
        LEFT JOIN checkin_user_themes AS owned
          ON owned.user_id = user.user_id
         AND owned.theme_id = user.current_theme_id
        WHERE owned.user_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if unowned_theme is not None:
        raise RuntimeError("旧版签到主题迁移校验失败")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise RuntimeError("旧版签到数据迁移后存在无效关联")

    for table in reversed(legacy_tables):
        conn.execute(f'DROP TABLE "{table}"')

    return LegacyMigrationSummary(
        users=users,
        records=records,
        preferences=preferences,
        purchases=purchases,
        removed_tables=legacy_tables,
        backup_path=backup_path,
    )


def _legacy_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    existing = {str(row[0]) for row in rows}
    return tuple(table for table in LEGACY_CHECKIN_TABLES if table in existing)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _theme_id_sql(expression: str) -> str:
    return (
        f"CASE {expression} "
        "WHEN '01' THEN 'blue' "
        "WHEN '02' THEN 'red' "
        "WHEN '03' THEN 'yellow' "
        "WHEN 'blue' THEN 'blue' "
        "WHEN 'red' THEN 'red' "
        "WHEN 'yellow' THEN 'yellow' "
        "ELSE 'default' END"
    )
