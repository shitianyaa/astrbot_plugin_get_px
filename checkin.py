from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import json
import random
import sqlite3
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CHECKIN_SNAPSHOT_SCHEMA_VERSION = 1
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

DUPLICATE_PENALTY = 0.20
DUPLICATE_DAILY_MAX = 1.00
MIN_AFFECTION = -10.0

BOOST_PRODUCTS: dict[int, int] = {
    1: 200,
    3: 500,
    7: 1000,
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
                "background_mode": "TEXT NOT NULL DEFAULT ''",
                "background_source": "TEXT NOT NULL DEFAULT ''",
                "background_illust_id": "TEXT NOT NULL DEFAULT ''",
                "background_title": "TEXT NOT NULL DEFAULT ''",
                "background_author": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in record_columns:
                    conn.execute(f"ALTER TABLE checkin_records ADD COLUMN {name} {ddl}")
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
        return {
            "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
            "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
            "scope": CHECKIN_SNAPSHOT_SCOPE,
            "exported_at": self.now_iso(),
            "profiles": profiles,
            "records": records,
        }

    def _import_snapshot_sync(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        normalized = _validate_checkin_snapshot(snapshot)
        profiles = normalized["profiles"]
        records = normalized["records"]

        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM checkin_records")
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
                            note, background_mode, background_source,
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
                            :note, :background_mode, :background_source,
                            :background_illust_id, :background_title,
                            :background_author, :created_at, :updated_at
                        )
                        """,
                        records,
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
        profile = self._get_or_create_profile_sync(user_id)
        if profile.last_checkin_date == date_key:
            return self._apply_duplicate_penalty_sync(date_key, now, profile)

        previous_date = _parse_date(profile.last_checkin_date)
        today = date.fromisoformat(date_key)
        if previous_date == today - timedelta(days=1):
            streak_days = profile.streak_days + 1
        else:
            streak_days = 1

        total_days = profile.total_days + 1
        base_coins, base_affection = _daily_base_reward(user_id, date_key)
        streak_steps = streak_days // STREAK_STEP_DAYS
        bonus_coins = min(streak_steps * STREAK_COIN_BONUS, STREAK_COIN_BONUS_MAX)
        bonus_affection = min(
            streak_steps * STREAK_AFFECTION_BONUS, STREAK_AFFECTION_BONUS_MAX
        )
        boost_active = is_boost_active(profile, date_key)
        affection_reward = round(base_affection + bonus_affection, 2)
        if boost_active:
            affection_reward = round(affection_reward * BOOST_MULTIPLIER, 2)
        coins_reward = base_coins + bonus_coins

        new_coins = profile.coins + coins_reward
        new_affection = round(profile.affection + affection_reward, 2)
        note = _daily_note(user_id, date_key, streak_days)

        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_profiles
                SET coins = ?, affection = ?, total_days = ?, streak_days = ?,
                    last_checkin_date = ?, repeat_penalty_date = ?,
                    repeat_penalty_total = 0, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    new_coins,
                    new_affection,
                    total_days,
                    streak_days,
                    date_key,
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
                ON CONFLICT(date_key, user_id) DO NOTHING
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
            conn.commit()

        updated = self._get_or_create_profile_sync(user_id)
        record = self._get_record_sync(date_key=date_key, user_id=user_id)
        return CheckinResult(profile=updated, record=record, duplicate=False)

    def _apply_duplicate_penalty_sync(
        self, date_key: str, now: str, profile: CheckinProfile
    ) -> CheckinResult:
        penalty_total = (
            profile.repeat_penalty_total
            if profile.repeat_penalty_date == date_key
            else 0.0
        )
        remaining_daily = max(0.0, DUPLICATE_DAILY_MAX - penalty_total)
        remaining_floor = max(0.0, profile.affection - MIN_AFFECTION)
        penalty = round(min(DUPLICATE_PENALTY, remaining_daily, remaining_floor), 2)
        new_penalty_total = round(penalty_total + penalty, 2)
        new_affection = round(profile.affection - penalty, 2)

        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_profiles
                SET affection = ?, repeat_penalty_date = ?,
                    repeat_penalty_total = ?, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    new_affection,
                    date_key,
                    new_penalty_total,
                    now,
                    profile.user_id,
                ),
            )
            conn.commit()

        updated = self._get_or_create_profile_sync(profile.user_id)
        record = self._get_record_sync(date_key=date_key, user_id=profile.user_id)
        return CheckinResult(
            profile=updated,
            record=record,
            duplicate=True,
            penalty_amount=penalty,
            penalty_total_today=new_penalty_total,
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
    if schema_version != CHECKIN_SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            f"不支持的签到备份版本: {schema_version!r}，当前仅支持 {CHECKIN_SNAPSHOT_SCHEMA_VERSION}"
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

    normalized_profiles = [
        _normalize_profile_snapshot_row(row, index) for index, row in enumerate(profiles)
    ]
    normalized_records = [
        _normalize_record_snapshot_row(row, index) for index, row in enumerate(records)
    ]
    return {
        "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
        "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
        "scope": CHECKIN_SNAPSHOT_SCOPE,
        "exported_at": exported_at,
        "profiles": normalized_profiles,
        "records": normalized_records,
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


def _require_text(row: dict[str, Any], key: str, location: str) -> str:
    if key not in row:
        raise ValueError(f"{location} 缺少字段 {key}")
    value = row.get(key)
    if value is None:
        return ""
    return str(value)


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
