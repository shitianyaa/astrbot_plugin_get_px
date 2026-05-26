from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Iterable
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class FortuneIndexRecord:
    date_key: str
    scope: str
    user_id: str
    source_key: str
    fortune_text: str
    illust_id: str
    image_source_key: str
    created_at: str
    updated_at: str


class ImageIndexStore:
    def __init__(self, data_dir: Path | str):
        self._db_path = Path(data_dir) / "image_index.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_db()

    @staticmethod
    def today_key() -> str:
        return datetime.now(SHANGHAI_TZ).date().isoformat()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    async def cleanup_old_days(self) -> None:
        date_key = self.today_key()
        async with self._lock:
            await asyncio.to_thread(self._cleanup_old_days_sync, date_key)

    async def get_used_illust_ids(self, scope: str, source_key: str) -> set[str]:
        date_key = self.today_key()
        async with self._lock:
            return await asyncio.to_thread(
                self._get_used_illust_ids_sync, date_key, scope, source_key
            )

    async def record_usage(
        self,
        *,
        scope: str,
        source_key: str,
        illust_id: str,
        feature: str,
        user_id: str = "",
    ) -> None:
        illust_id = str(illust_id or "")
        if not illust_id:
            return
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            await asyncio.to_thread(
                self._record_usage_sync,
                date_key,
                scope,
                source_key,
                illust_id,
                feature,
                user_id,
                now,
            )

    async def claim_usage(
        self,
        *,
        scope: str,
        source_key: str,
        illust_id: str,
        feature: str,
        user_id: str = "",
    ) -> bool:
        illust_id = str(illust_id or "")
        if not illust_id:
            return False
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_usage_sync,
                date_key,
                scope,
                source_key,
                illust_id,
                feature,
                user_id,
                now,
            )

    async def release_usage(
        self,
        *,
        scope: str,
        source_key: str,
        illust_id: str,
        feature: str,
    ) -> None:
        illust_id = str(illust_id or "")
        if not illust_id or not feature:
            return
        date_key = self.today_key()
        async with self._lock:
            await asyncio.to_thread(
                self._release_usage_sync,
                date_key,
                scope,
                source_key,
                illust_id,
                feature,
            )

    async def get_fortune_record(
        self, *, scope: str, user_id: str, source_key: str
    ) -> FortuneIndexRecord | None:
        date_key = self.today_key()
        async with self._lock:
            return await asyncio.to_thread(
                self._get_fortune_record_sync, date_key, scope, user_id, source_key
            )

    async def save_fortune_record(
        self,
        *,
        scope: str,
        user_id: str,
        source_key: str,
        fortune_text: str,
        illust_id: str = "",
        image_source_key: str = "",
    ) -> None:
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            await asyncio.to_thread(
                self._save_fortune_record_sync,
                date_key,
                scope,
                user_id,
                source_key,
                fortune_text,
                str(illust_id or ""),
                str(image_source_key or ""),
                now,
            )

    async def claim_fortune_illust_id(
        self,
        *,
        scope: str,
        user_id: str,
        source_key: str,
        illust_id: str,
        image_source_key: str = "",
    ) -> FortuneIndexRecord | None:
        illust_id = str(illust_id or "")
        if not illust_id:
            return None
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._claim_fortune_illust_id_sync,
                date_key,
                scope,
                user_id,
                source_key,
                illust_id,
                str(image_source_key or ""),
                now,
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS image_usage (
                    date_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    illust_id TEXT NOT NULL,
                    feature TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY (date_key, scope, source_key, illust_id)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_image_usage_lookup
                ON image_usage (date_key, scope, source_key)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fortune_records (
                    date_key TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    source_key TEXT NOT NULL,
                    fortune_text TEXT NOT NULL,
                    illust_id TEXT NOT NULL DEFAULT '',
                    image_source_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (date_key, scope, user_id, source_key)
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(fortune_records)").fetchall()
            }
            if "image_source_key" not in columns:
                conn.execute(
                    """
                    ALTER TABLE fortune_records
                    ADD COLUMN image_source_key TEXT NOT NULL DEFAULT ''
                    """
                )
            conn.commit()

    def _cleanup_old_days_sync(self, date_key: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM image_usage WHERE date_key != ?", (date_key,))
            conn.execute("DELETE FROM fortune_records WHERE date_key != ?", (date_key,))
            conn.commit()

    def _get_used_illust_ids_sync(
        self, date_key: str, scope: str, source_key: str
    ) -> set[str]:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT illust_id FROM image_usage
                WHERE date_key = ? AND scope = ? AND source_key = ?
                """,
                (date_key, scope, source_key),
            ).fetchall()
        return {str(row["illust_id"]) for row in rows}

    def _record_usage_sync(
        self,
        date_key: str,
        scope: str,
        source_key: str,
        illust_id: str,
        feature: str,
        user_id: str,
        sent_at: str,
    ) -> None:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO image_usage (
                    date_key, scope, source_key, illust_id, feature, user_id, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_key, scope, source_key, illust_id)
                DO UPDATE SET
                    feature = excluded.feature,
                    user_id = excluded.user_id,
                    sent_at = excluded.sent_at
                """,
                (date_key, scope, source_key, illust_id, feature, user_id, sent_at),
            )
            conn.commit()

    def _claim_usage_sync(
        self,
        date_key: str,
        scope: str,
        source_key: str,
        illust_id: str,
        feature: str,
        user_id: str,
        sent_at: str,
    ) -> bool:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO image_usage (
                    date_key, scope, source_key, illust_id, feature, user_id, sent_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (date_key, scope, source_key, illust_id, feature, user_id, sent_at),
            )
            conn.commit()
            return cursor.rowcount > 0

    def _release_usage_sync(
        self,
        date_key: str,
        scope: str,
        source_key: str,
        illust_id: str,
        feature: str,
    ) -> None:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                DELETE FROM image_usage
                WHERE date_key = ? AND scope = ? AND source_key = ?
                    AND illust_id = ? AND feature = ?
                """,
                (date_key, scope, source_key, illust_id, feature),
            )
            conn.commit()

    def _get_fortune_record_sync(
        self, date_key: str, scope: str, user_id: str, source_key: str
    ) -> FortuneIndexRecord | None:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM fortune_records
                WHERE date_key = ? AND scope = ? AND user_id = ? AND source_key = ?
                """,
                (date_key, scope, user_id, source_key),
            ).fetchone()
        if row is None:
            return None
        return FortuneIndexRecord(
            date_key=str(row["date_key"]),
            scope=str(row["scope"]),
            user_id=str(row["user_id"]),
            source_key=str(row["source_key"]),
            fortune_text=str(row["fortune_text"]),
            illust_id=str(row["illust_id"] or ""),
            image_source_key=str(row["image_source_key"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _save_fortune_record_sync(
        self,
        date_key: str,
        scope: str,
        user_id: str,
        source_key: str,
        fortune_text: str,
        illust_id: str,
        image_source_key: str,
        now: str,
    ) -> None:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO fortune_records (
                    date_key, scope, user_id, source_key,
                    fortune_text, illust_id, image_source_key, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date_key, scope, user_id, source_key)
                DO NOTHING
                """,
                (
                    date_key,
                    scope,
                    user_id,
                    source_key,
                    fortune_text,
                    illust_id,
                    image_source_key,
                    now,
                    now,
                ),
            )
            conn.commit()

    def _claim_fortune_illust_id_sync(
        self,
        date_key: str,
        scope: str,
        user_id: str,
        source_key: str,
        illust_id: str,
        image_source_key: str,
        now: str,
    ) -> FortuneIndexRecord | None:
        self._cleanup_old_days_sync(date_key)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE fortune_records
                SET illust_id = ?, image_source_key = ?, updated_at = ?
                WHERE date_key = ? AND scope = ? AND user_id = ? AND source_key = ?
                    AND (illust_id = '' OR illust_id IS NULL)
                """,
                (
                    illust_id,
                    image_source_key,
                    now,
                    date_key,
                    scope,
                    user_id,
                    source_key,
                ),
            )
            conn.commit()
        return self._get_fortune_record_sync(date_key, scope, user_id, source_key)


def ordered_by_unused(illusts: Iterable[dict], used_ids: set[str]) -> list[dict]:
    fresh: list[dict] = []
    repeated: list[dict] = []
    for illust in illusts:
        illust_id = str(illust.get("id", ""))
        if illust_id and illust_id in used_ids:
            repeated.append(illust)
        else:
            fresh.append(illust)
    return fresh + repeated
