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
LEGACY_CHECKIN_RECORDS_TABLE = "_checkin_records_pre_v3"

_SUPPORTED_LEGACY_THEME_IDS = (
    "default",
    "01",
    "02",
    "03",
    "blue",
    "red",
    "yellow",
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


def stage_legacy_checkin_records(conn: sqlite3.Connection) -> str | None:
    existing = _table_names(conn)
    if "checkin_records" not in existing:
        return None
    if LEGACY_CHECKIN_RECORDS_TABLE in existing:
        raise RuntimeError("旧版签到记录暂存表已存在，无法安全迁移")
    conn.execute(
        f'ALTER TABLE "checkin_records" RENAME TO "{LEGACY_CHECKIN_RECORDS_TABLE}"'
    )
    conn.execute("DROP INDEX IF EXISTS idx_checkin_records_member_updated")
    return LEGACY_CHECKIN_RECORDS_TABLE


def migrate_legacy_checkin_data(
    conn: sqlite3.Connection,
    *,
    legacy_tables: tuple[str, ...],
    legacy_records_table: str | None,
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
    records = (
        _table_count(conn, legacy_records_table)
        if legacy_records_table is not None
        else 0
    )
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
        if "selected_theme_id" in preference_columns:
            unsupported_theme = conn.execute(
                """
                SELECT selected_theme_id
                FROM checkin_user_preferences
                WHERE selected_theme_id IS NULL
                   OR selected_theme_id NOT IN ('default', '01', '02', '03')
                LIMIT 1
                """
            ).fetchone()
            if unsupported_theme is not None:
                raise RuntimeError(
                    "旧版签到偏好记录包含未知主题: "
                    f"{unsupported_theme[0]!s}"
                )
        orphaned_preference = conn.execute(
            """
            SELECT preference.user_id
            FROM checkin_user_preferences AS preference
            LEFT JOIN checkin_users AS user ON user.user_id = preference.user_id
            WHERE user.user_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if orphaned_preference is not None:
            raise RuntimeError(
                "旧版签到偏好记录缺少对应用户: "
                f"{orphaned_preference[0]!s}"
            )
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
        unsupported_theme = conn.execute(
            """
            SELECT theme_id
            FROM checkin_theme_purchases
            WHERE theme_id IS NULL
               OR theme_id NOT IN ('default', '01', '02', '03')
            LIMIT 1
            """
        ).fetchone()
        if unsupported_theme is not None:
            raise RuntimeError(
                "旧版签到主题购买记录包含未知主题: "
                f"{unsupported_theme[0]!s}"
            )
        orphaned_purchase = conn.execute(
            """
            SELECT purchase.user_id
            FROM checkin_theme_purchases AS purchase
            LEFT JOIN checkin_users AS user ON user.user_id = purchase.user_id
            WHERE user.user_id IS NULL
            LIMIT 1
            """
        ).fetchone()
        if orphaned_purchase is not None:
            raise RuntimeError(
                "旧版签到主题购买记录缺少对应用户: "
                f"{orphaned_purchase[0]!s}"
            )
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
    if legacy_records_table is not None:
        _migrate_legacy_records(conn, legacy_records_table)

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
    if legacy_records_table is not None:
        conn.execute(f'DROP TABLE "{legacy_records_table}"')

    return LegacyMigrationSummary(
        users=users,
        records=records,
        preferences=preferences,
        purchases=purchases,
        removed_tables=legacy_tables,
        backup_path=backup_path,
    )


def _legacy_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    existing = _table_names(conn)
    return tuple(table for table in LEGACY_CHECKIN_TABLES if table in existing)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _migrate_legacy_records(conn: sqlite3.Connection, source_table: str) -> None:
    source_columns = _table_columns(conn, source_table)
    required_columns = {"date_key", "user_id", "created_at", "updated_at"}
    missing = required_columns - source_columns
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(f"旧版签到记录表缺少必要字段: {names}")

    theme_expression = (
        'source."theme_id"' if "theme_id" in source_columns else "'default'"
    )
    if "theme_id" in source_columns:
        placeholders = ", ".join("?" for _ in _SUPPORTED_LEGACY_THEME_IDS)
        unsupported_theme = conn.execute(
            f"""
            SELECT source.theme_id
            FROM "{source_table}" AS source
            WHERE source.theme_id IS NULL
               OR source.theme_id NOT IN ({placeholders})
            LIMIT 1
            """,
            _SUPPORTED_LEGACY_THEME_IDS,
        ).fetchone()
        if unsupported_theme is not None:
            raise RuntimeError(
                "旧版签到记录包含未知主题: " f"{unsupported_theme[0]!s}"
            )

    orphaned_user = conn.execute(
        f"""
        SELECT source.user_id
        FROM "{source_table}" AS source
        LEFT JOIN checkin_users AS user ON user.user_id = source.user_id
        WHERE user.user_id IS NULL
        LIMIT 1
        """
    ).fetchone()
    if orphaned_user is not None:
        raise RuntimeError(
            "旧版签到记录缺少对应用户: " f"{orphaned_user[0]!s}"
        )

    columns_with_defaults = (
        ("date_key", None),
        ("user_id", None),
        ("username", "''"),
        ("bot_name", "''"),
        ("base_coins", "0"),
        ("bonus_coins", "0"),
        ("coins_reward", "0"),
        ("base_affection", "0"),
        ("bonus_affection", "0"),
        ("affection_reward", "0"),
        ("boost_active", "0"),
        ("boost_multiplier", "1"),
        ("total_coins_after", "0"),
        ("total_affection_after", "0"),
        ("total_days_after", "0"),
        ("streak_days_after", "0"),
        ("note", "''"),
        ("event_key", "''"),
        ("event_label", "''"),
        ("greeting", "''"),
        ("greeting_source", "'local'"),
        ("greeting_attribution", "''"),
        ("secondary_note", "''"),
        ("template_version", None),
        ("theme_id", None),
        ("background_mode", "''"),
        ("background_source", "''"),
        ("background_illust_id", "''"),
        ("background_title", "''"),
        ("background_author", "''"),
        ("created_at", None),
        ("updated_at", None),
    )
    target_columns = [name for name, _ in columns_with_defaults]
    select_expressions: list[str] = []
    for name, fallback in columns_with_defaults:
        if name == "theme_id":
            select_expressions.append(_theme_id_sql(theme_expression))
        elif name == "template_version":
            template_expression = (
                'source."template_version"'
                if "template_version" in source_columns
                else "''"
            )
            select_expressions.append(
                "CASE "
                f"WHEN {template_expression} LIKE '%:%' THEN {template_expression} "
                f"WHEN {theme_expression} IN ('01', 'blue') THEN 'blue:1' "
                f"WHEN {theme_expression} IN ('02', 'red') THEN 'red:1' "
                f"WHEN {theme_expression} IN ('03', 'yellow') THEN 'yellow:1' "
                "ELSE 'default:1' END"
            )
        elif name in source_columns:
            select_expressions.append(f'source."{name}"')
        elif fallback is not None:
            select_expressions.append(fallback)
        else:
            raise RuntimeError(f"旧版签到记录表缺少必要字段: {name}")

    conn.execute(
        f"""
        INSERT INTO checkin_records ({", ".join(target_columns)})
        SELECT {", ".join(select_expressions)}
        FROM "{source_table}" AS source
        """
    )


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
