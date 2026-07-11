from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import random
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CHECKIN_SNAPSHOT_SCHEMA_VERSION = 3
CHECKIN_SNAPSHOT_SCOPE = "checkin"
CHECKIN_SNAPSHOT_PLUGIN_NAME = "astrbot_plugin_get_px"

BASE_COIN_MIN = 50
BASE_COIN_MAX = 100
BASE_AFFECTION_MIN = 0.50
BASE_AFFECTION_MAX = 1.20
STREAK_STEP_DAYS = 7
STREAK_COIN_BONUS = 10
STREAK_COIN_BONUS_MAX = 50
STREAK_AFFECTION_BONUS = 0.10
STREAK_AFFECTION_BONUS_MAX = 0.50
BOOST_MULTIPLIER = 2.0

MIN_AFFECTION = -10.0

BOOST_PRODUCTS: dict[int, int] = {
    1: 200,
    3: 500,
    7: 1000,
}

ACHIEVEMENTS: dict[str, dict[str, Any]] = {
    "first_meeting": {"title": "初见旅人", "kind": "total", "threshold": 1},
    "streak_7": {"title": "七日同行", "kind": "streak", "threshold": 7},
    "total_30": {"title": "月下常客", "kind": "total", "threshold": 30},
    "total_100": {"title": "百日珍藏", "kind": "total", "threshold": 100},
    "total_365": {"title": "周年相守", "kind": "total", "threshold": 365},
    "total_1000": {"title": "千日物语", "kind": "total", "threshold": 1000},
}


@dataclass(frozen=True)
class CheckinProfile:
    user_id: str
    coins: int
    affection: float
    total_days: int
    streak_days: int
    last_checkin_date: str
    boost_start_date: str
    boost_until_date: str
    repeat_penalty_date: str
    repeat_penalty_total: float
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CheckinRecord:
    date_key: str
    user_id: str
    username: str
    bot_name: str
    base_coins: int
    bonus_coins: int
    coins_reward: int
    base_affection: float
    bonus_affection: float
    affection_reward: float
    boost_active: bool
    boost_multiplier: float
    total_coins_after: int
    total_affection_after: float
    total_days_after: int
    streak_days_after: int
    note: str
    background_mode: str
    background_source: str
    background_illust_id: str
    background_title: str
    background_author: str
    created_at: str
    updated_at: str
    event_key: str = ""
    event_label: str = ""
    greeting: str = ""
    greeting_source: str = "local"
    secondary_note: str = ""
    template_version: str = "v2"


@dataclass(frozen=True)
class CheckinResult:
    profile: CheckinProfile
    record: CheckinRecord | None
    duplicate: bool
    penalty_amount: float = 0.0
    penalty_total_today: float = 0.0


@dataclass(frozen=True)
class BoostPurchaseResult:
    success: bool
    profile: CheckinProfile
    days: int
    cost: int
    message: str


@dataclass(frozen=True)
class CheckinUserPreference:
    user_id: str
    birthday_month: int
    birthday_day: int
    birthday_source: str
    qq_birthday_checked: bool
    selected_title_id: str
    created_at: str
    updated_at: str

    @property
    def birthday_label(self) -> str:
        if self.birthday_month <= 0 or self.birthday_day <= 0:
            return ""
        return f"{self.birthday_month:02d}-{self.birthday_day:02d}"


@dataclass(frozen=True)
class CheckinGlobalEvent:
    event_id: int
    event_type: str
    date_value: str
    name: str
    created_by: str
    created_at: str
    updated_at: str


class CheckinStore:
    def __init__(self, data_dir: Path | str):
        self._db_path = Path(data_dir) / "checkin.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_db()

    @staticmethod
    def today_key() -> str:
        return datetime.now(SHANGHAI_TZ).date().isoformat()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    async def get_profile(self, user_id: str) -> CheckinProfile:
        user_id = str(user_id or "")
        async with self._lock:
            return await asyncio.to_thread(self._get_or_create_profile_sync, user_id)

    async def get_today_record(self, user_id: str) -> CheckinRecord | None:
        user_id = str(user_id or "")
        date_key = self.today_key()
        async with self._lock:
            return await asyncio.to_thread(
                self._get_record_sync, date_key=date_key, user_id=user_id
            )

    async def checkin(
        self, *, user_id: str, username: str, bot_name: str
    ) -> CheckinResult:
        user_id = str(user_id or "")
        if not user_id:
            raise ValueError("user_id is required")
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._checkin_sync,
                date_key,
                now,
                user_id,
                str(username or user_id),
                str(bot_name or "neko"),
            )

    async def purchase_boost(self, *, user_id: str, days: int) -> BoostPurchaseResult:
        user_id = str(user_id or "")
        if days not in BOOST_PRODUCTS:
            profile = await self.get_profile(user_id)
            return BoostPurchaseResult(
                success=False,
                profile=profile,
                days=days,
                cost=0,
                message="可购买的加持天数只有 1、3、7 天。",
            )
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._purchase_boost_sync, date_key, now, user_id, days
            )

    async def get_user_preference(self, user_id: str) -> CheckinUserPreference:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_or_create_preference_sync, str(user_id or "")
            )

    async def set_birthday(
        self, *, user_id: str, month: int, day: int, source: str
    ) -> CheckinUserPreference:
        _validate_month_day(month, day)
        if source not in {"manual", "qq"}:
            raise ValueError("birthday source must be manual or qq")
        async with self._lock:
            return await asyncio.to_thread(
                self._set_birthday_sync, str(user_id or ""), month, day, source
            )

    async def set_qq_birthday_if_not_manual(
        self, *, user_id: str, month: int, day: int
    ) -> CheckinUserPreference:
        _validate_month_day(month, day)
        async with self._lock:
            return await asyncio.to_thread(
                self._set_qq_birthday_if_not_manual_sync,
                str(user_id or ""),
                month,
                day,
            )

    async def clear_birthday(self, user_id: str) -> CheckinUserPreference:
        async with self._lock:
            return await asyncio.to_thread(
                self._clear_birthday_sync, str(user_id or "")
            )

    async def mark_qq_birthday_checked(self, user_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mark_qq_birthday_checked_sync, str(user_id or "")
            )

    async def unlock_achievements(self, profile: CheckinProfile) -> tuple[str, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._unlock_achievements_sync, profile)

    async def list_achievements(self, user_id: str) -> tuple[str, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_achievements_sync, str(user_id or "")
            )

    async def select_title(self, *, user_id: str, title: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(
                self._select_title_sync, str(user_id or ""), str(title or "")
            )

    async def add_global_event(
        self, *, event_type: str, date_value: str, name: str, created_by: str
    ) -> CheckinGlobalEvent:
        normalized_type, normalized_date = _validate_global_event_date(
            event_type, date_value
        )
        clean_name = _clean_event_name(name)
        async with self._lock:
            return await asyncio.to_thread(
                self._add_global_event_sync,
                normalized_type,
                normalized_date,
                clean_name,
                str(created_by or ""),
            )

    async def list_global_events(self) -> tuple[CheckinGlobalEvent, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_global_events_sync)

    async def delete_global_event(self, event_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._delete_global_event_sync, int(event_id))

    async def events_for_date(self, date_key: str) -> tuple[CheckinGlobalEvent, ...]:
        day = date.fromisoformat(date_key)
        async with self._lock:
            return await asyncio.to_thread(
                self._events_for_date_sync, day.isoformat(), day.strftime("%m-%d")
            )

    async def update_record_background(
        self,
        *,
        user_id: str,
        date_key: str,
        mode: str,
        source: str = "",
        illust_id: str = "",
        title: str = "",
        author: str = "",
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_record_background_sync,
                str(user_id or ""),
                str(date_key or ""),
                str(mode or ""),
                str(source or ""),
                str(illust_id or ""),
                str(title or ""),
                str(author or ""),
                self.now_iso(),
            )

    async def update_record_content(
        self,
        *,
        user_id: str,
        date_key: str,
        event_key: str,
        event_label: str,
        greeting: str,
        greeting_source: str,
        secondary_note: str,
        template_version: str,
    ) -> CheckinRecord:
        """Persist local content once and permit only a local-to-AI upgrade."""
        validated_source = _validate_greeting_source(
            greeting_source, "greeting_source"
        )
        async with self._lock:
            return await asyncio.to_thread(
                self._update_record_content_sync,
                str(user_id or ""),
                str(date_key or ""),
                str(event_key or ""),
                str(event_label or ""),
                str(greeting or ""),
                validated_source,
                str(secondary_note or ""),
                str(template_version or "v2"),
                self.now_iso(),
            )

    async def export_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._export_snapshot_sync)

    async def import_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._import_snapshot_sync, snapshot)

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
                    secondary_note TEXT NOT NULL DEFAULT '',
                    template_version TEXT NOT NULL DEFAULT 'v2',
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
                "secondary_note": "TEXT NOT NULL DEFAULT ''",
                "template_version": "TEXT NOT NULL DEFAULT 'v2'",
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
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
            conn.commit()

    def _export_snapshot_sync(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            profiles = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_profiles ORDER BY user_id"
                ).fetchall()
            ]
            records = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_records ORDER BY date_key, user_id"
                ).fetchall()
            ]
            preferences = [dict(row) for row in conn.execute(
                "SELECT * FROM checkin_user_preferences ORDER BY user_id"
            ).fetchall()]
            global_events = [dict(row) for row in conn.execute(
                "SELECT * FROM checkin_global_events ORDER BY event_id"
            ).fetchall()]
            achievements = [dict(row) for row in conn.execute(
                "SELECT * FROM checkin_achievements ORDER BY user_id, achievement_id"
            ).fetchall()]
        return {
            "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
            "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
            "scope": CHECKIN_SNAPSHOT_SCOPE,
            "exported_at": self.now_iso(),
            "profiles": profiles,
            "records": records,
            "preferences": preferences,
            "global_events": global_events,
            "achievements": achievements,
        }

    def _import_snapshot_sync(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        normalized = _validate_checkin_snapshot(snapshot)
        profiles = normalized["profiles"]
        records = normalized["records"]
        preferences = normalized["preferences"]
        global_events = normalized["global_events"]
        achievements = normalized["achievements"]

        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM checkin_records")
                conn.execute("DELETE FROM checkin_achievements")
                conn.execute("DELETE FROM checkin_global_events")
                conn.execute("DELETE FROM checkin_user_preferences")
                conn.execute("DELETE FROM checkin_profiles")
                if profiles:
                    conn.executemany(
                        """
                        INSERT INTO checkin_profiles (
                            user_id, coins, affection, total_days, streak_days,
                            last_checkin_date, boost_start_date, boost_until_date,
                            repeat_penalty_date, repeat_penalty_total,
                            created_at, updated_at
                        )
                        VALUES (
                            :user_id, :coins, :affection, :total_days, :streak_days,
                            :last_checkin_date, :boost_start_date, :boost_until_date,
                            :repeat_penalty_date, :repeat_penalty_total,
                            :created_at, :updated_at
                        )
                        """,
                        profiles,
                    )
                if records:
                    conn.executemany(
                        """
                        INSERT INTO checkin_records (
                            date_key, user_id, username, bot_name,
                            base_coins, bonus_coins, coins_reward,
                            base_affection, bonus_affection, affection_reward,
                            boost_active, boost_multiplier,
                            total_coins_after, total_affection_after,
                            total_days_after, streak_days_after,
                            note, event_key, event_label, greeting,
                            greeting_source, secondary_note, template_version,
                            background_mode, background_source,
                            background_illust_id, background_title,
                            background_author, created_at, updated_at
                        )
                        VALUES (
                            :date_key, :user_id, :username, :bot_name,
                            :base_coins, :bonus_coins, :coins_reward,
                            :base_affection, :bonus_affection, :affection_reward,
                            :boost_active, :boost_multiplier,
                            :total_coins_after, :total_affection_after,
                            :total_days_after, :streak_days_after,
                            :note, :event_key, :event_label, :greeting,
                            :greeting_source, :secondary_note, :template_version,
                            :background_mode, :background_source,
                            :background_illust_id, :background_title,
                            :background_author, :created_at, :updated_at
                        )
                        """,
                        records,
                    )
                if preferences:
                    conn.executemany(
                        """
                        INSERT INTO checkin_user_preferences
                        (user_id, birthday_month, birthday_day, birthday_source,
                         qq_birthday_checked, selected_title_id, created_at, updated_at)
                        VALUES (:user_id, :birthday_month, :birthday_day, :birthday_source,
                                :qq_birthday_checked, :selected_title_id, :created_at, :updated_at)
                        """, preferences,
                    )
                if global_events:
                    conn.executemany(
                        """
                        INSERT INTO checkin_global_events
                        (event_id, event_type, date_value, name, created_by, created_at, updated_at)
                        VALUES (:event_id, :event_type, :date_value, :name, :created_by, :created_at, :updated_at)
                        """, global_events,
                    )
                if achievements:
                    conn.executemany(
                        """
                        INSERT INTO checkin_achievements (user_id, achievement_id, unlocked_at)
                        VALUES (:user_id, :achievement_id, :unlocked_at)
                        """, achievements,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "schema_version": normalized["schema_version"],
            "plugin_name": normalized["plugin_name"],
            "scope": normalized["scope"],
            "profiles": len(profiles),
            "records": len(records),
            "preferences": len(preferences),
            "global_events": len(global_events),
            "achievements": len(achievements),
            "exported_at": normalized["exported_at"],
            "imported_at": self.now_iso(),
        }

    def _get_or_create_profile_sync(self, user_id: str) -> CheckinProfile:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is not None:
                return self._row_to_profile(row)
            now = self.now_iso()
            conn.execute(
                """
                INSERT INTO checkin_profiles (
                    user_id, coins, affection, total_days, streak_days,
                    last_checkin_date, boost_start_date, boost_until_date,
                    repeat_penalty_date, repeat_penalty_total, created_at, updated_at
                )
                VALUES (?, 0, 0, 0, 0, '', '', '', '', 0, ?, ?)
                """,
                (user_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._row_to_profile(row)

    def _checkin_sync(
        self, date_key: str, now: str, user_id: str, username: str, bot_name: str
    ) -> CheckinResult:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO checkin_profiles (
                        user_id, coins, affection, total_days, streak_days,
                        last_checkin_date, boost_start_date, boost_until_date,
                        repeat_penalty_date, repeat_penalty_total,
                        created_at, updated_at
                    )
                    VALUES (?, 0, 0, 0, 0, '', '', '', '', 0, ?, ?)
                    """,
                    (user_id, now, now),
                )
                profile_row = conn.execute(
                    "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                profile = self._row_to_profile(profile_row)
                record_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE date_key = ? AND user_id = ?
                    """,
                    (date_key, user_id),
                ).fetchone()
                if record_row is not None:
                    conn.commit()
                    return CheckinResult(
                        profile=profile,
                        record=self._row_to_record(record_row),
                        duplicate=True,
                    )

                previous_date = _parse_date(profile.last_checkin_date)
                today = date.fromisoformat(date_key)
                if previous_date == today - timedelta(days=1):
                    streak_days = profile.streak_days + 1
                else:
                    streak_days = 1

                total_days = profile.total_days + 1
                base_coins, base_affection = _daily_base_reward(user_id, date_key)
                streak_steps = streak_days // STREAK_STEP_DAYS
                bonus_coins = min(
                    streak_steps * STREAK_COIN_BONUS, STREAK_COIN_BONUS_MAX
                )
                bonus_affection = min(
                    streak_steps * STREAK_AFFECTION_BONUS,
                    STREAK_AFFECTION_BONUS_MAX,
                )
                boost_active = is_boost_active(profile, date_key)
                affection_reward = round(base_affection + bonus_affection, 2)
                if boost_active:
                    affection_reward = round(
                        affection_reward * BOOST_MULTIPLIER, 2
                    )
                coins_reward = base_coins + bonus_coins
                new_coins = profile.coins + coins_reward
                new_affection = round(profile.affection + affection_reward, 2)
                note = _daily_note(user_id, date_key, streak_days)

                conn.execute(
                    """
                    UPDATE checkin_profiles
                    SET coins = ?, affection = ?, total_days = ?, streak_days = ?,
                        last_checkin_date = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        new_coins,
                        new_affection,
                        total_days,
                        streak_days,
                        date_key,
                        now,
                        user_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO checkin_records (
                        date_key, user_id, username, bot_name,
                        base_coins, bonus_coins, coins_reward,
                        base_affection, bonus_affection, affection_reward,
                        boost_active, boost_multiplier,
                        total_coins_after, total_affection_after,
                        total_days_after, streak_days_after,
                        note, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date_key,
                        user_id,
                        username,
                        bot_name,
                        base_coins,
                        bonus_coins,
                        coins_reward,
                        base_affection,
                        bonus_affection,
                        affection_reward,
                        1 if boost_active else 0,
                        BOOST_MULTIPLIER if boost_active else 1.0,
                        new_coins,
                        new_affection,
                        total_days,
                        streak_days,
                        note,
                        now,
                        now,
                    ),
                )
                updated_row = conn.execute(
                    "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                record_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE date_key = ? AND user_id = ?
                    """,
                    (date_key, user_id),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return CheckinResult(
            profile=self._row_to_profile(updated_row),
            record=self._row_to_record(record_row),
            duplicate=False,
        )

    def _purchase_boost_sync(
        self, date_key: str, now: str, user_id: str, days: int
    ) -> BoostPurchaseResult:
        cost = BOOST_PRODUCTS[days]
        profile = self._get_or_create_profile_sync(user_id)
        if profile.coins < cost:
            return BoostPurchaseResult(
                success=False,
                profile=profile,
                days=days,
                cost=cost,
                message=f"金币不足，需要 {cost}，当前只有 {profile.coins}。",
            )

        today = date.fromisoformat(date_key)
        signed_today = profile.last_checkin_date == date_key
        requested_start = today + timedelta(days=1 if signed_today else 0)
        current_until = _parse_date(profile.boost_until_date)
        current_start = _parse_date(profile.boost_start_date)
        if current_until is not None and current_until >= requested_start:
            start_date = current_start or requested_start
            until_date = current_until + timedelta(days=days)
        else:
            start_date = requested_start
            until_date = requested_start + timedelta(days=days - 1)

        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_profiles
                SET coins = ?, boost_start_date = ?, boost_until_date = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    profile.coins - cost,
                    start_date.isoformat(),
                    until_date.isoformat(),
                    now,
                    user_id,
                ),
            )
            conn.commit()

        updated = self._get_or_create_profile_sync(user_id)
        start_label = "今天" if start_date == today else start_date.isoformat()
        message = (
            f"购买成功，消耗 {cost} 金币。好感度双倍加持从 {start_label} "
            f"生效，到 {until_date.isoformat()} 结束。"
        )
        return BoostPurchaseResult(True, updated, days, cost, message)

    def _get_record_sync(self, *, date_key: str, user_id: str) -> CheckinRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM checkin_records
                WHERE date_key = ? AND user_id = ?
                """,
                (date_key, user_id),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def _update_record_background_sync(
        self,
        user_id: str,
        date_key: str,
        mode: str,
        source: str,
        illust_id: str,
        title: str,
        author: str,
        now: str,
    ) -> None:
        if not user_id or not date_key:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_records
                SET background_mode = ?, background_source = ?,
                    background_illust_id = ?, background_title = ?,
                    background_author = ?, updated_at = ?
                WHERE user_id = ? AND date_key = ?
                """,
                (mode, source, illust_id, title, author, now, user_id, date_key),
            )
            conn.commit()

    def _update_record_content_sync(
        self,
        user_id: str,
        date_key: str,
        event_key: str,
        event_label: str,
        greeting: str,
        greeting_source: str,
        secondary_note: str,
        template_version: str,
        now: str,
    ) -> CheckinRecord:
        if not user_id or not date_key:
            raise ValueError("user_id and date_key are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET event_key = ?, event_label = ?, greeting = ?,
                        greeting_source = ?, secondary_note = ?,
                        template_version = ?, updated_at = ?
                    WHERE user_id = ? AND date_key = ?
                      AND NOT (? = '' AND ? = '' AND ? = '' AND ? = '')
                      AND (
                        (
                            ? = 'local'
                            AND greeting_source = 'local'
                            AND event_key = '' AND event_label = ''
                            AND greeting = '' AND secondary_note = ''
                        )
                        OR
                        (
                            ? = 'ai' AND greeting_source = 'local'
                            AND NOT (
                                event_key = '' AND event_label = ''
                                AND greeting = '' AND secondary_note = ''
                            )
                        )
                      )
                    """,
                    (
                        event_key,
                        event_label,
                        greeting,
                        greeting_source,
                        secondary_note,
                        template_version,
                        now,
                        user_id,
                        date_key,
                        event_key,
                        event_label,
                        greeting,
                        secondary_note,
                        greeting_source,
                        greeting_source,
                    ),
                )
                updated_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE user_id = ? AND date_key = ?
                    """,
                    (user_id, date_key),
                ).fetchone()
                if updated_row is None:
                    raise ValueError("check-in record not found")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._row_to_record(updated_row)

    def _get_or_create_preference_sync(self, user_id: str) -> CheckinUserPreference:
        if not user_id:
            raise ValueError("user_id is required")
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO checkin_user_preferences
                (user_id, created_at, updated_at) VALUES (?, ?, ?)
                """,
                (user_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM checkin_user_preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._row_to_preference(row)

    def _set_birthday_sync(
        self, user_id: str, month: int, day: int, source: str
    ) -> CheckinUserPreference:
        self._get_or_create_preference_sync(user_id)
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_user_preferences
                SET birthday_month = ?, birthday_day = ?, birthday_source = ?,
                    qq_birthday_checked = 1, updated_at = ? WHERE user_id = ?
                """,
                (month, day, source, now, user_id),
            )
            conn.commit()
        return self._get_or_create_preference_sync(user_id)

    def _set_qq_birthday_if_not_manual_sync(
        self, user_id: str, month: int, day: int
    ) -> CheckinUserPreference:
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE checkin_user_preferences
                SET birthday_month = ?, birthday_day = ?, birthday_source = 'qq',
                    qq_birthday_checked = 1, updated_at = ?
                WHERE user_id = ? AND birthday_source != 'manual'
                """,
                (month, day, self.now_iso(), user_id),
            )
            conn.commit()
        return self._get_or_create_preference_sync(user_id)

    def _clear_birthday_sync(self, user_id: str) -> CheckinUserPreference:
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_user_preferences SET birthday_month = 0,
                    birthday_day = 0, birthday_source = '', qq_birthday_checked = 0,
                    updated_at = ? WHERE user_id = ?
                """,
                (self.now_iso(), user_id),
            )
            conn.commit()
        return self._get_or_create_preference_sync(user_id)

    def _mark_qq_birthday_checked_sync(self, user_id: str) -> None:
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE checkin_user_preferences SET qq_birthday_checked = 1, updated_at = ? WHERE user_id = ?",
                (self.now_iso(), user_id),
            )
            conn.commit()

    def _unlock_achievements_sync(self, profile: CheckinProfile) -> tuple[str, ...]:
        now = self.now_iso()
        unlocked: list[str] = []
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for achievement_id, definition in ACHIEVEMENTS.items():
                value = profile.total_days if definition["kind"] == "total" else profile.streak_days
                if value < int(definition["threshold"]):
                    continue
                changed = conn.execute(
                    "INSERT OR IGNORE INTO checkin_achievements (user_id, achievement_id, unlocked_at) VALUES (?, ?, ?)",
                    (profile.user_id, achievement_id, now),
                ).rowcount
                if changed:
                    unlocked.append(achievement_id)
            preference = conn.execute(
                "SELECT selected_title_id FROM checkin_user_preferences WHERE user_id = ?",
                (profile.user_id,),
            ).fetchone()
            if preference is None:
                conn.execute(
                    "INSERT INTO checkin_user_preferences (user_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (profile.user_id, now, now),
                )
                selected = ""
            else:
                selected = str(preference["selected_title_id"] or "")
            if not selected and unlocked:
                conn.execute(
                    "UPDATE checkin_user_preferences SET selected_title_id = ?, updated_at = ? WHERE user_id = ?",
                    (unlocked[0], now, profile.user_id),
                )
            conn.commit()
        return tuple(unlocked)

    def _list_achievements_sync(self, user_id: str) -> tuple[str, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT achievement_id FROM checkin_achievements WHERE user_id = ? ORDER BY unlocked_at, achievement_id",
                (user_id,),
            ).fetchall()
        return tuple(str(row["achievement_id"]) for row in rows)

    def _select_title_sync(self, user_id: str, title: str) -> str:
        title_id = title if title in ACHIEVEMENTS else next(
            (key for key, value in ACHIEVEMENTS.items() if value["title"] == title), ""
        )
        if not title_id:
            raise ValueError("未知称号")
        if title_id not in self._list_achievements_sync(user_id):
            raise ValueError("该称号尚未解锁")
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE checkin_user_preferences SET selected_title_id = ?, updated_at = ? WHERE user_id = ?",
                (title_id, self.now_iso(), user_id),
            )
            conn.commit()
        return title_id

    def _add_global_event_sync(
        self, event_type: str, date_value: str, name: str, created_by: str
    ) -> CheckinGlobalEvent:
        now = self.now_iso()
        with closing(self._connect()) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO checkin_global_events
                    (event_type, date_value, name, created_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event_type, date_value, name, created_by, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("该日期已存在同类型事件") from exc
            row = conn.execute(
                "SELECT * FROM checkin_global_events WHERE event_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_global_event(row)

    def _list_global_events_sync(self) -> tuple[CheckinGlobalEvent, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM checkin_global_events ORDER BY event_type, date_value, event_id"
            ).fetchall()
        return tuple(self._row_to_global_event(row) for row in rows)

    def _delete_global_event_sync(self, event_id: int) -> bool:
        with closing(self._connect()) as conn:
            changed = conn.execute(
                "DELETE FROM checkin_global_events WHERE event_id = ?", (event_id,)
            ).rowcount
            conn.commit()
        return bool(changed)

    def _events_for_date_sync(
        self, exact_date: str, annual_date: str
    ) -> tuple[CheckinGlobalEvent, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM checkin_global_events
                WHERE (event_type = 'once' AND date_value = ?)
                   OR (event_type = 'annual' AND date_value = ?)
                ORDER BY CASE event_type WHEN 'once' THEN 0 ELSE 1 END, event_id
                """,
                (exact_date, annual_date),
            ).fetchall()
        return tuple(self._row_to_global_event(row) for row in rows)

    @staticmethod
    def _row_to_preference(row: sqlite3.Row) -> CheckinUserPreference:
        return CheckinUserPreference(
            user_id=str(row["user_id"]),
            birthday_month=int(row["birthday_month"] or 0),
            birthday_day=int(row["birthday_day"] or 0),
            birthday_source=str(row["birthday_source"] or ""),
            qq_birthday_checked=bool(row["qq_birthday_checked"]),
            selected_title_id=str(row["selected_title_id"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    @staticmethod
    def _row_to_global_event(row: sqlite3.Row) -> CheckinGlobalEvent:
        return CheckinGlobalEvent(
            event_id=int(row["event_id"]), event_type=str(row["event_type"]),
            date_value=str(row["date_value"]), name=str(row["name"]),
            created_by=str(row["created_by"] or ""),
            created_at=str(row["created_at"]), updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> CheckinProfile:
        return CheckinProfile(
            user_id=str(row["user_id"]),
            coins=int(row["coins"] or 0),
            affection=round(float(row["affection"] or 0), 2),
            total_days=int(row["total_days"] or 0),
            streak_days=int(row["streak_days"] or 0),
            last_checkin_date=str(row["last_checkin_date"] or ""),
            boost_start_date=str(row["boost_start_date"] or ""),
            boost_until_date=str(row["boost_until_date"] or ""),
            repeat_penalty_date=str(row["repeat_penalty_date"] or ""),
            repeat_penalty_total=round(float(row["repeat_penalty_total"] or 0), 2),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CheckinRecord:
        return CheckinRecord(
            date_key=str(row["date_key"]),
            user_id=str(row["user_id"]),
            username=str(row["username"] or ""),
            bot_name=str(row["bot_name"] or ""),
            base_coins=int(row["base_coins"] or 0),
            bonus_coins=int(row["bonus_coins"] or 0),
            coins_reward=int(row["coins_reward"] or 0),
            base_affection=round(float(row["base_affection"] or 0), 2),
            bonus_affection=round(float(row["bonus_affection"] or 0), 2),
            affection_reward=round(float(row["affection_reward"] or 0), 2),
            boost_active=bool(row["boost_active"]),
            boost_multiplier=round(float(row["boost_multiplier"] or 1), 2),
            total_coins_after=int(row["total_coins_after"] or 0),
            total_affection_after=round(float(row["total_affection_after"] or 0), 2),
            total_days_after=int(row["total_days_after"] or 0),
            streak_days_after=int(row["streak_days_after"] or 0),
            note=str(row["note"] or ""),
            background_mode=str(row["background_mode"] or ""),
            background_source=str(row["background_source"] or ""),
            background_illust_id=str(row["background_illust_id"] or ""),
            background_title=str(row["background_title"] or ""),
            background_author=str(row["background_author"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            event_key=str(row["event_key"] or ""),
            event_label=str(row["event_label"] or ""),
            greeting=str(row["greeting"] or ""),
            greeting_source=str(row["greeting_source"] or "local"),
            secondary_note=str(row["secondary_note"] or ""),
            template_version=str(row["template_version"] or "v2"),
        )


def affection_level(value: float) -> dict[str, Any]:
    levels = [
        (MIN_AFFECTION, 0.0, "排斥"),
        (0.0, 10.0, "陌生"),
        (10.0, 30.0, "熟悉"),
        (30.0, 70.0, "亲近"),
        (70.0, 140.0, "信赖"),
        (140.0, None, "挚友"),
    ]
    for lower, upper, name in levels:
        if upper is None or value < upper:
            if upper is None:
                progress = 100
                next_value = None
            else:
                span = upper - lower
                progress = int(max(0, min(100, ((value - lower) / span) * 100)))
                next_value = upper
            return {
                "name": name,
                "lower": lower,
                "upper": upper,
                "next": next_value,
                "progress": progress,
            }
    return {"name": "挚友", "lower": 140.0, "upper": None, "next": None, "progress": 100}


def is_boost_active(profile: CheckinProfile, date_key: str | None = None) -> bool:
    date_key = date_key or CheckinStore.today_key()
    today = date.fromisoformat(date_key)
    start = _parse_date(profile.boost_start_date)
    until = _parse_date(profile.boost_until_date)
    return bool(start and until and start <= today <= until)


def boost_remaining_days(profile: CheckinProfile, date_key: str | None = None) -> int:
    date_key = date_key or CheckinStore.today_key()
    today = date.fromisoformat(date_key)
    until = _parse_date(profile.boost_until_date)
    if until is None or until < today:
        return 0
    return (until - today).days + 1


def boost_status_text(profile: CheckinProfile, date_key: str | None = None) -> str:
    date_key = date_key or CheckinStore.today_key()
    today = date.fromisoformat(date_key)
    start = _parse_date(profile.boost_start_date)
    until = _parse_date(profile.boost_until_date)
    if start is None or until is None or until < today:
        return "无加持"
    remaining = (until - max(today, start)).days + 1
    if start > today:
        return f"{start.isoformat()} 生效，剩余 {remaining} 天"
    return f"生效中，剩余 {remaining} 天"


def _daily_base_reward(user_id: str, date_key: str) -> tuple[int, float]:
    seed_text = f"checkin-reward|{date_key}|{user_id}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    coins = rng.randint(BASE_COIN_MIN, BASE_COIN_MAX)
    affection = round(rng.uniform(BASE_AFFECTION_MIN, BASE_AFFECTION_MAX), 2)
    return coins, affection


def _daily_note(user_id: str, date_key: str, streak_days: int) -> str:
    notes = [
        "明天也要来哦",
        "今天也有好好见面",
        "脚步很轻，但确实在靠近",
        "连续记录被认真收好了",
        "今天的心情也闪了一下",
    ]
    if streak_days and streak_days % 7 == 0:
        return f"连续 {streak_days} 天，奖励加成已生效"
    seed_text = f"checkin-note|{date_key}|{user_id}"
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    return notes[seed % len(notes)]


def _parse_date(value: str) -> date | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def dump_checkin_snapshot_json(snapshot: dict[str, Any]) -> str:
    normalized = _validate_checkin_snapshot(snapshot)
    return json.dumps(normalized, ensure_ascii=False, indent=2)


def load_checkin_snapshot_json(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig")
    else:
        text = str(raw)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("签到备份文件不是合法的 JSON") from exc
    return _validate_checkin_snapshot(data)


def _validate_checkin_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise ValueError("签到备份数据必须是对象")

    schema_version = snapshot.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in (1, 2, CHECKIN_SNAPSHOT_SCHEMA_VERSION)
    ):
        raise ValueError(
            f"不支持的签到备份版本: {schema_version!r}，当前支持 1、2 和 {CHECKIN_SNAPSHOT_SCHEMA_VERSION}"
        )

    plugin_name = str(snapshot.get("plugin_name") or "").strip()
    if plugin_name != CHECKIN_SNAPSHOT_PLUGIN_NAME:
        raise ValueError("签到备份文件不属于当前插件")

    scope = str(snapshot.get("scope") or "").strip()
    if scope != CHECKIN_SNAPSHOT_SCOPE:
        raise ValueError("签到备份文件作用域不正确")

    exported_at = str(snapshot.get("exported_at") or "").strip()
    if not exported_at:
        raise ValueError("签到备份缺少 exported_at")

    profiles = snapshot.get("profiles")
    if not isinstance(profiles, list):
        raise ValueError("签到备份 profiles 必须是数组")

    records = snapshot.get("records")
    if not isinstance(records, list):
        raise ValueError("签到备份 records 必须是数组")
    preferences = snapshot.get("preferences") if schema_version >= 3 else []
    global_events = snapshot.get("global_events") if schema_version >= 3 else []
    achievements = snapshot.get("achievements") if schema_version >= 3 else []
    for key, value in (("preferences", preferences), ("global_events", global_events), ("achievements", achievements)):
        if not isinstance(value, list):
            raise ValueError(f"签到备份 {key} 必须是数组")

    normalized_profiles = [
        _normalize_profile_snapshot_row(row, index) for index, row in enumerate(profiles)
    ]
    normalized_records = [
        _normalize_record_snapshot_row(row, index) for index, row in enumerate(records)
    ]
    normalized_preferences = [_normalize_preference_snapshot_row(row, index) for index, row in enumerate(preferences)]
    normalized_events = [_normalize_global_event_snapshot_row(row, index) for index, row in enumerate(global_events)]
    normalized_achievements = [_normalize_achievement_snapshot_row(row, index) for index, row in enumerate(achievements)]
    profile_user_ids: set[str] = set()
    for index, profile in enumerate(normalized_profiles):
        user_id = profile["user_id"]
        if user_id in profile_user_ids:
            raise ValueError(f"profiles[{index}].user_id duplicate: {user_id}")
        profile_user_ids.add(user_id)

    record_keys: set[tuple[str, str]] = set()
    for index, record in enumerate(normalized_records):
        record_key = (record["date_key"], record["user_id"])
        if record_key in record_keys:
            raise ValueError(
                f"records[{index}].date_key and user_id duplicate: "
                f"{record_key[0]}, {record_key[1]}"
            )
        record_keys.add(record_key)
        if record["user_id"] not in profile_user_ids:
            raise ValueError(
                f"records[{index}].user_id has no matching profile: "
                f"{record['user_id']}"
            )
    preference_ids: set[str] = set()
    for index, preference in enumerate(normalized_preferences):
        user_id = preference["user_id"]
        if user_id in preference_ids:
            raise ValueError(f"preferences[{index}].user_id duplicate: {user_id}")
        preference_ids.add(user_id)
    achievement_keys: set[tuple[str, str]] = set()
    for index, achievement in enumerate(normalized_achievements):
        key = (achievement["user_id"], achievement["achievement_id"])
        if key in achievement_keys:
            raise ValueError(f"achievements[{index}] duplicate")
        achievement_keys.add(key)
        if achievement["user_id"] not in profile_user_ids:
            raise ValueError(f"achievements[{index}].user_id has no matching profile")
    event_ids: set[int] = set()
    event_dates: set[tuple[str, str]] = set()
    for index, event in enumerate(normalized_events):
        if event["event_id"] in event_ids or (event["event_type"], event["date_value"]) in event_dates:
            raise ValueError(f"global_events[{index}] duplicate")
        event_ids.add(event["event_id"])
        event_dates.add((event["event_type"], event["date_value"]))
    for index, preference in enumerate(normalized_preferences):
        selected = preference["selected_title_id"]
        if selected and (preference["user_id"], selected) not in achievement_keys:
            raise ValueError(f"preferences[{index}].selected_title_id 未解锁")
    return {
        "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
        "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
        "scope": CHECKIN_SNAPSHOT_SCOPE,
        "exported_at": exported_at,
        "profiles": normalized_profiles,
        "records": normalized_records,
        "preferences": normalized_preferences,
        "global_events": normalized_events,
        "achievements": normalized_achievements,
    }


def _normalize_profile_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"profiles[{index}] 必须是对象")
    return {
        "user_id": _require_text(row, "user_id", f"profiles[{index}]"),
        "coins": _require_int(row, "coins", f"profiles[{index}]"),
        "affection": _require_float(row, "affection", f"profiles[{index}]"),
        "total_days": _require_int(row, "total_days", f"profiles[{index}]"),
        "streak_days": _require_int(row, "streak_days", f"profiles[{index}]"),
        "last_checkin_date": _require_text(
            row, "last_checkin_date", f"profiles[{index}]"
        ),
        "boost_start_date": _require_text(
            row, "boost_start_date", f"profiles[{index}]"
        ),
        "boost_until_date": _require_text(
            row, "boost_until_date", f"profiles[{index}]"
        ),
        "repeat_penalty_date": _require_text(
            row, "repeat_penalty_date", f"profiles[{index}]"
        ),
        "repeat_penalty_total": _require_float(
            row, "repeat_penalty_total", f"profiles[{index}]"
        ),
        "created_at": _require_text(row, "created_at", f"profiles[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"profiles[{index}]"),
    }


def _normalize_record_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"records[{index}] 必须是对象")
    return {
        "date_key": _require_text(row, "date_key", f"records[{index}]"),
        "user_id": _require_text(row, "user_id", f"records[{index}]"),
        "username": _require_text(row, "username", f"records[{index}]"),
        "bot_name": _require_text(row, "bot_name", f"records[{index}]"),
        "base_coins": _require_int(row, "base_coins", f"records[{index}]"),
        "bonus_coins": _require_int(row, "bonus_coins", f"records[{index}]"),
        "coins_reward": _require_int(row, "coins_reward", f"records[{index}]"),
        "base_affection": _require_float(
            row, "base_affection", f"records[{index}]"
        ),
        "bonus_affection": _require_float(
            row, "bonus_affection", f"records[{index}]"
        ),
        "affection_reward": _require_float(
            row, "affection_reward", f"records[{index}]"
        ),
        "boost_active": _require_boolish_int(
            row, "boost_active", f"records[{index}]"
        ),
        "boost_multiplier": _require_float(
            row, "boost_multiplier", f"records[{index}]"
        ),
        "total_coins_after": _require_int(
            row, "total_coins_after", f"records[{index}]"
        ),
        "total_affection_after": _require_float(
            row, "total_affection_after", f"records[{index}]"
        ),
        "total_days_after": _require_int(
            row, "total_days_after", f"records[{index}]"
        ),
        "streak_days_after": _require_int(
            row, "streak_days_after", f"records[{index}]"
        ),
        "note": _require_text(row, "note", f"records[{index}]"),
        "event_key": _optional_text(row, "event_key", ""),
        "event_label": _optional_text(row, "event_label", ""),
        "greeting": _optional_text(row, "greeting", ""),
        "greeting_source": _validate_greeting_source(
            _optional_text(row, "greeting_source", "local"),
            f"records[{index}].greeting_source",
        ),
        "secondary_note": _optional_text(row, "secondary_note", ""),
        "template_version": _optional_text(row, "template_version", "v2"),
        "background_mode": _require_text(
            row, "background_mode", f"records[{index}]"
        ),
        "background_source": _require_text(
            row, "background_source", f"records[{index}]"
        ),
        "background_illust_id": _require_text(
            row, "background_illust_id", f"records[{index}]"
        ),
        "background_title": _require_text(
            row, "background_title", f"records[{index}]"
        ),
        "background_author": _require_text(
            row, "background_author", f"records[{index}]"
        ),
        "created_at": _require_text(row, "created_at", f"records[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"records[{index}]"),
    }


def _normalize_preference_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"preferences[{index}] 必须是对象")
    month = _require_int(row, "birthday_month", f"preferences[{index}]")
    day = _require_int(row, "birthday_day", f"preferences[{index}]")
    source = _optional_text(row, "birthday_source", "")
    if (month, day) != (0, 0):
        _validate_month_day(month, day)
        if source not in {"manual", "qq"}:
            raise ValueError(f"preferences[{index}].birthday_source 无效")
    selected = _optional_text(row, "selected_title_id", "")
    if selected and selected not in ACHIEVEMENTS:
        raise ValueError(f"preferences[{index}].selected_title_id 无效")
    return {
        "user_id": _require_text(row, "user_id", f"preferences[{index}]"),
        "birthday_month": month, "birthday_day": day, "birthday_source": source,
        "qq_birthday_checked": _require_boolish_int(row, "qq_birthday_checked", f"preferences[{index}]"),
        "selected_title_id": selected,
        "created_at": _require_text(row, "created_at", f"preferences[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"preferences[{index}]"),
    }


def _normalize_global_event_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"global_events[{index}] 必须是对象")
    event_type, date_value = _validate_global_event_date(
        _require_text(row, "event_type", f"global_events[{index}]"),
        _require_text(row, "date_value", f"global_events[{index}]"),
    )
    return {
        "event_id": _require_int(row, "event_id", f"global_events[{index}]"),
        "event_type": event_type, "date_value": date_value,
        "name": _clean_event_name(_require_text(row, "name", f"global_events[{index}]")),
        "created_by": _optional_text(row, "created_by", ""),
        "created_at": _require_text(row, "created_at", f"global_events[{index}]"),
        "updated_at": _require_text(row, "updated_at", f"global_events[{index}]"),
    }


def _normalize_achievement_snapshot_row(row: Any, index: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise ValueError(f"achievements[{index}] 必须是对象")
    achievement_id = _require_text(row, "achievement_id", f"achievements[{index}]")
    if achievement_id not in ACHIEVEMENTS:
        raise ValueError(f"achievements[{index}].achievement_id 无效")
    return {
        "user_id": _require_text(row, "user_id", f"achievements[{index}]"),
        "achievement_id": achievement_id,
        "unlocked_at": _require_text(row, "unlocked_at", f"achievements[{index}]"),
    }


def _require_text(row: dict[str, Any], key: str, location: str) -> str:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if value is None:
        return ""
    return str(value)


def _optional_text(row: dict[str, Any], key: str, default: str) -> str:
    value = row.get(key, default)
    if value is None:
        return default
    return str(value)


def _validate_greeting_source(value: Any, location: str) -> str:
    source = "" if value is None else str(value)
    if source not in ("local", "ai"):
        raise ValueError(f"{location} 仅允许 local/ai")
    return source


def _validate_month_day(month: int, day: int) -> None:
    try:
        date(2000, int(month), int(day))
    except (TypeError, ValueError) as exc:
        raise ValueError("生日格式无效，请使用 MM-DD") from exc


def _validate_global_event_date(event_type: str, value: str) -> tuple[str, str]:
    normalized_type = str(event_type or "").strip().lower()
    normalized_value = str(value or "").strip()
    if normalized_type == "annual":
        try:
            month_text, day_text = normalized_value.split("-", 1)
            _validate_month_day(int(month_text), int(day_text))
            return normalized_type, f"{int(month_text):02d}-{int(day_text):02d}"
        except (ValueError, TypeError) as exc:
            raise ValueError("年度事件日期必须为 MM-DD") from exc
    if normalized_type == "once":
        try:
            return normalized_type, date.fromisoformat(normalized_value).isoformat()
        except ValueError as exc:
            raise ValueError("单次事件日期必须为 YYYY-MM-DD") from exc
    raise ValueError("事件类型仅允许 annual/once")


def _clean_event_name(value: object) -> str:
    raw = str(value or "")
    cleaned = "".join(
        (" " if char.isspace() else "")
        if unicodedata.category(char) in {"Cc", "Cf"}
        else char
        for char in raw
    )
    text = " ".join(cleaned.split())
    if not text:
        raise ValueError("事件名称不能为空")
    if len(text) > 20:
        raise ValueError("事件名称最多 20 个字符")
    return text


def _require_int(row: dict[str, Any], key: str, location: str) -> int:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{location}.{key} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是整数") from exc


def _require_float(row: dict[str, Any], key: str, location: str) -> float:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool):
        raise ValueError(f"{location}.{key} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是数字") from exc


def _require_boolish_int(row: dict[str, Any], key: str, location: str) -> int:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if isinstance(value, bool):
        return 1 if value else 0
    try:
        int_value = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{location}.{key} 必须是布尔值或 0/1") from exc
    if int_value not in (0, 1):
        raise ValueError(f"{location}.{key} 只能是 0 或 1")
    return int_value
