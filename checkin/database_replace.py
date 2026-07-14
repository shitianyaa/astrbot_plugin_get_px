from __future__ import annotations

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .schema import CHECKIN_DB_SCHEMA_VERSION
from .themes import CHECKIN_THEMES


MAX_CHECKIN_DATABASE_BYTES = 64 * 1024 * 1024
CHECKIN_DATABASE_SUFFIXES = {".db", ".sqlite3"}

_REQUIRED_COLUMNS = {
    "checkin_themes": {
        "theme_id",
        "code",
        "name",
        "description",
        "version",
        "price",
        "enabled",
        "sort_order",
    },
    "checkin_users": {
        "user_id",
        "coins",
        "affection",
        "total_days",
        "streak_days",
        "last_checkin_date",
        "boost_start_date",
        "boost_until_date",
        "repeat_penalty_date",
        "repeat_penalty_total",
        "birthday_month",
        "birthday_day",
        "birthday_source",
        "qq_birthday_checked",
        "selected_title_id",
        "current_theme_id",
        "created_at",
        "updated_at",
    },
    "checkin_user_themes": {
        "user_id",
        "theme_id",
        "price_paid",
        "acquired_at",
    },
    "checkin_records": {
        "date_key",
        "user_id",
        "username",
        "bot_name",
        "base_coins",
        "bonus_coins",
        "coins_reward",
        "base_affection",
        "bonus_affection",
        "affection_reward",
        "boost_active",
        "boost_multiplier",
        "total_coins_after",
        "total_affection_after",
        "total_days_after",
        "streak_days_after",
        "note",
        "event_key",
        "event_label",
        "greeting",
        "greeting_source",
        "greeting_attribution",
        "secondary_note",
        "template_version",
        "theme_id",
        "background_mode",
        "background_source",
        "background_illust_id",
        "background_title",
        "background_author",
        "created_at",
        "updated_at",
    },
    "checkin_global_events": {
        "event_id",
        "event_type",
        "date_value",
        "name",
        "created_by",
        "created_at",
        "updated_at",
    },
    "checkin_achievements": {"user_id", "achievement_id", "unlocked_at"},
    "checkin_group_presence": {
        "date_key",
        "group_id",
        "group_name",
        "platform",
        "user_id",
        "username",
        "first_seen_at",
        "last_seen_at",
    },
}

_LEGACY_TABLES = {
    "checkin_profiles",
    "checkin_user_preferences",
    "checkin_theme_purchases",
}


def stage_uploaded_checkin_database(upload: Any, data_dir: Path) -> Path:
    content_length = getattr(upload, "content_length", None)
    if isinstance(content_length, int) and content_length > MAX_CHECKIN_DATABASE_BYTES:
        raise ValueError("签到数据库文件不能超过 64 MiB")

    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / f".checkin-import-{time.time_ns()}.sqlite3"
    total = 0
    try:
        with path.open("xb") as handle:
            while chunk := upload.stream.read(64 * 1024):
                total += len(chunk)
                if total > MAX_CHECKIN_DATABASE_BYTES:
                    raise ValueError("签到数据库文件不能超过 64 MiB")
                handle.write(chunk)
        if total == 0:
            raise ValueError("签到数据库文件不能为空")
        return path
    except Exception:
        path.unlink(missing_ok=True)
        raise


async def replace_checkin_database(store: Any, candidate: Path) -> dict[str, int]:
    async with store._lock:
        return await asyncio.to_thread(
            _replace_checkin_database_sync,
            Path(store._db_path),
            Path(candidate),
        )


def validate_checkin_database(path: Path) -> dict[str, int]:
    try:
        with path.open("rb") as handle:
            if handle.read(16) != b"SQLite format 3\x00":
                raise ValueError("上传文件不是有效的 SQLite 数据库")
    except OSError as exc:
        raise ValueError("无法读取上传的签到数据库") from exc

    try:
        uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise ValueError("签到数据库完整性检查失败")

            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version != CHECKIN_DB_SCHEMA_VERSION:
                raise ValueError(
                    f"签到数据库版本必须为 {CHECKIN_DB_SCHEMA_VERSION}，"
                    f"当前为 {version}"
                )

            objects = conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            tables = {str(row["name"]) for row in objects if row["type"] == "table"}
            forbidden = _LEGACY_TABLES & tables
            if forbidden:
                raise ValueError("上传文件仍包含旧版签到数据表")
            unexpected_code = [
                str(row["name"])
                for row in objects
                if row["type"] in {"trigger", "view"}
            ]
            if unexpected_code:
                raise ValueError("签到数据库不能包含触发器或视图")

            for table, required in _REQUIRED_COLUMNS.items():
                if table not in tables:
                    raise ValueError(f"签到数据库缺少数据表 {table}")
                columns = {
                    str(row["name"])
                    for row in conn.execute(f'PRAGMA table_info("{table}")')
                }
                missing = required - columns
                if missing:
                    raise ValueError(f"签到数据库表 {table} 缺少必要字段")

            foreign_key_error = conn.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_error is not None:
                raise ValueError("签到数据库存在无效的数据关联")

            theme_ids = {
                str(row[0]) for row in conn.execute("SELECT theme_id FROM checkin_themes")
            }
            if not set(CHECKIN_THEMES).issubset(theme_ids):
                raise ValueError("签到数据库缺少内置主题")

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
                raise ValueError("签到数据库存在未拥有的当前主题")

            return {
                "users": int(conn.execute("SELECT COUNT(*) FROM checkin_users").fetchone()[0]),
                "records": int(conn.execute("SELECT COUNT(*) FROM checkin_records").fetchone()[0]),
                "group_presence": int(
                    conn.execute("SELECT COUNT(*) FROM checkin_group_presence").fetchone()[0]
                ),
                "user_themes": int(
                    conn.execute("SELECT COUNT(*) FROM checkin_user_themes").fetchone()[0]
                ),
            }
    except sqlite3.DatabaseError as exc:
        raise ValueError("上传文件不是可读取的签到数据库") from exc


def _replace_checkin_database_sync(target: Path, candidate: Path) -> dict[str, int]:
    summary = validate_checkin_database(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        with closing(sqlite3.connect(target)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    sidecars = (
        Path(f"{target}-wal"),
        Path(f"{target}-shm"),
        Path(f"{target}-journal"),
    )
    for sidecar in sidecars:
        sidecar.unlink(missing_ok=True)

    os.replace(candidate, target)
    validate_checkin_database(target)
    return summary
