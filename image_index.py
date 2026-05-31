from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


class ImageIndexStore:
    def __init__(self, data_dir: Path | str):
        self._db_path = Path(data_dir) / "image_index.sqlite3"
        self._blacklist_thumb_dir = Path(data_dir) / "image_blacklist" / "thumbs"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._blacklist_thumb_dir.mkdir(parents=True, exist_ok=True)
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

    async def get_blacklisted_illust_ids(self) -> set[str]:
        async with self._lock:
            return await asyncio.to_thread(self._get_blacklisted_illust_ids_sync)

    async def list_blacklist_illusts(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_blacklist_illusts_sync)

    async def is_blacklisted(self, illust_id: str) -> bool:
        illust_id = str(illust_id or "")
        if not illust_id:
            return False
        async with self._lock:
            return await asyncio.to_thread(self._is_blacklisted_sync, illust_id)

    async def get_blacklist_thumbnail_path(self, illust_id: str) -> Path | None:
        illust_id = str(illust_id or "")
        if not illust_id:
            return None
        async with self._lock:
            return await asyncio.to_thread(
                self._get_blacklist_thumbnail_path_sync, illust_id
            )

    async def get_blacklist_thumbnail_paths(
        self, illust_ids: Iterable[str]
    ) -> dict[str, Path]:
        ids = self._normalize_ids(illust_ids)
        if not ids:
            return {}
        async with self._lock:
            return await asyncio.to_thread(
                self._get_blacklist_thumbnail_paths_sync, ids
            )

    async def save_blacklist_thumbnail(
        self, *, illust_id: str, source_path: Path | str | None
    ) -> str:
        illust_id = str(illust_id or "")
        if not illust_id or not source_path:
            return ""
        async with self._lock:
            return await asyncio.to_thread(
                self._save_blacklist_thumbnail_sync, illust_id, Path(source_path)
            )

    async def add_blacklist_illust(
        self,
        *,
        illust_id: str,
        title: str = "",
        author: str = "",
        source: str = "",
        record_id: str = "",
        thumb_id: str = "",
    ) -> bool:
        illust_id = str(illust_id or "")
        if not illust_id:
            return False
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._add_blacklist_illust_sync,
                illust_id,
                str(title or ""),
                str(author or ""),
                str(source or ""),
                str(record_id or ""),
                str(thumb_id or ""),
                now,
            )

    async def remove_blacklist_illust(self, illust_id: str) -> dict[str, Any] | None:
        illust_id = str(illust_id or "")
        if not illust_id:
            return None
        async with self._lock:
            return await asyncio.to_thread(
                self._remove_blacklist_illust_sync, illust_id
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
                CREATE TABLE IF NOT EXISTS image_blacklist (
                    illust_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    author TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    record_id TEXT NOT NULL DEFAULT '',
                    thumb_id TEXT NOT NULL DEFAULT '',
                    added_at TEXT NOT NULL
                )
                """
            )
            blacklist_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(image_blacklist)").fetchall()
            }
            if "thumb_id" not in blacklist_columns:
                conn.execute(
                    """
                    ALTER TABLE image_blacklist
                    ADD COLUMN thumb_id TEXT NOT NULL DEFAULT ''
                    """
                )
            conn.commit()

    def _cleanup_old_days_sync(self, date_key: str) -> None:
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM image_usage WHERE date_key != ?", (date_key,))
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

    def _get_blacklisted_illust_ids_sync(self) -> set[str]:
        with closing(self._connect()) as conn:
            rows = conn.execute("SELECT illust_id FROM image_blacklist").fetchall()
        return {str(row["illust_id"]) for row in rows}

    def _list_blacklist_illusts_sync(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT illust_id, title, author, source, record_id, thumb_id, added_at
                FROM image_blacklist
                ORDER BY added_at DESC, illust_id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def _is_blacklisted_sync(self, illust_id: str) -> bool:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM image_blacklist WHERE illust_id = ? LIMIT 1",
                (illust_id,),
            ).fetchone()
        return row is not None

    def _get_blacklist_thumbnail_path_sync(self, illust_id: str) -> Path | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT thumb_id FROM image_blacklist WHERE illust_id = ? LIMIT 1",
                (illust_id,),
            ).fetchone()
        if row is None:
            return None
        thumb_id = str(row["thumb_id"] or "")
        if not thumb_id:
            return None
        path = (self._blacklist_thumb_dir / Path(thumb_id).name).resolve()
        thumb_dir = self._blacklist_thumb_dir.resolve()
        if thumb_dir not in path.parents:
            return None
        return path if path.exists() else None

    def _get_blacklist_thumbnail_paths_sync(
        self, illust_ids: list[str]
    ) -> dict[str, Path]:
        placeholders = ",".join("?" for _ in illust_ids)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT illust_id, thumb_id FROM image_blacklist
                WHERE illust_id IN ({placeholders})
                """,
                illust_ids,
            ).fetchall()
        thumb_dir = self._blacklist_thumb_dir.resolve()
        paths: dict[str, Path] = {}
        for row in rows:
            illust_id = str(row["illust_id"] or "")
            thumb_id = str(row["thumb_id"] or "")
            if not illust_id or not thumb_id:
                continue
            path = (self._blacklist_thumb_dir / Path(thumb_id).name).resolve()
            if thumb_dir not in path.parents:
                continue
            if path.exists():
                paths[illust_id] = path
        return paths

    def _save_blacklist_thumbnail_sync(self, illust_id: str, source_path: Path) -> str:
        if not source_path.is_file():
            return ""
        thumb_id = f"{illust_id}.jpg"
        dest_path = self._blacklist_thumb_dir / thumb_id
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".tmp")
        self._blacklist_thumb_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, tmp_path)
        tmp_path.replace(dest_path)
        return thumb_id

    def _add_blacklist_illust_sync(
        self,
        illust_id: str,
        title: str,
        author: str,
        source: str,
        record_id: str,
        thumb_id: str,
        added_at: str,
    ) -> bool:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                INSERT INTO image_blacklist (
                    illust_id, title, author, source, record_id, thumb_id, added_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(illust_id)
                DO UPDATE SET
                    title = excluded.title,
                    author = excluded.author,
                    source = excluded.source,
                    record_id = excluded.record_id,
                    thumb_id = CASE
                        WHEN excluded.thumb_id != '' THEN excluded.thumb_id
                        ELSE image_blacklist.thumb_id
                    END,
                    added_at = excluded.added_at
                """,
                (illust_id, title, author, source, record_id, thumb_id, added_at),
            )
            conn.commit()
            return cursor.rowcount > 0

    def _remove_blacklist_illust_sync(self, illust_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT illust_id, title, author, source, record_id, thumb_id, added_at
                FROM image_blacklist
                WHERE illust_id = ?
                """,
                (illust_id,),
            ).fetchone()
            if row is None:
                return None
            record = dict(row)
            conn.execute(
                "DELETE FROM image_blacklist WHERE illust_id = ?", (illust_id,)
            )
            conn.commit()
        thumb_id = str(record.get("thumb_id") or "")
        if thumb_id:
            self._safe_unlink(self._blacklist_thumb_dir / Path(thumb_id).name)
        return record

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _normalize_ids(values: Iterable[str]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for value in values:
            item = str(value or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            ids.append(item)
        return ids

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
