from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sqlite3
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .safety import normalize_safety_text


LOG_PREFIX = "[GetPx]"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
PAGE_CURSOR_TTL_DAYS = 3
MIN_RESULTS_FOR_NEXT_PAGE = 20
_CLEANUP_THROTTLE_SECONDS = 3600  # 每小时最多清理一次旧记录


class ImageIndexStore:
    def __init__(self, data_dir: Path | str):
        self._db_path = Path(data_dir) / "image_index.sqlite3"
        self._blacklist_thumb_dir = Path(data_dir) / "image_blacklist" / "thumbs"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._blacklist_thumb_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._last_cleanup_ts: float = 0.0
        self._conn: sqlite3.Connection | None = None
        self._init_db()

    @staticmethod
    def today_key() -> str:
        return datetime.now(SHANGHAI_TZ).date().isoformat()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")

    @staticmethod
    def _now_dt() -> datetime:
        """返回当前上海时间，可被子类覆写以冻结时间。"""
        return datetime.now(SHANGHAI_TZ)

    def _get_conn(self) -> sqlite3.Connection:
        """获取持久数据库连接；如已关闭则自动重建。"""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    def close(self) -> None:
        """关闭数据库连接，释放资源。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

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

    async def get_page_offset(self, scope: str, source_key: str) -> int:
        """获取指定 scope + source_key 的分页游标，未记录时返回 0。"""
        async with self._lock:
            return await asyncio.to_thread(
                self._get_page_offset_sync, scope, source_key
            )

    async def advance_page_offset(
        self, scope: str, source_key: str, results_count: int
    ) -> int:
        """当前页全被用过后推进游标；若结果数明显少于正常页则重置为 0（已到底，循环）。返回新的 offset。"""
        async with self._lock:
            return await asyncio.to_thread(
                self._advance_page_offset_sync, scope, source_key, results_count
            )

    async def get_blacklisted_illust_ids(self) -> set[str]:
        async with self._lock:
            return await asyncio.to_thread(self._get_blacklisted_illust_ids_sync)

    async def list_safety_terms(self) -> list[dict[str, str]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_safety_terms_sync)

    async def add_safety_term(self, term: str, *, added_by: str = "web") -> bool:
        term = str(term or "").strip()
        normalized = normalize_safety_text(term)
        if not normalized:
            raise ValueError("屏蔽词不能为空")
        if len(term) > 64:
            raise ValueError("屏蔽词不能超过 64 个字符")
        async with self._lock:
            return await asyncio.to_thread(
                self._add_safety_term_sync, term, normalized, added_by, self.now_iso()
            )

    async def remove_safety_term(self, term: str) -> bool:
        normalized = normalize_safety_text(term)
        if not normalized:
            raise ValueError("屏蔽词不能为空")
        async with self._lock:
            return await asyncio.to_thread(self._remove_safety_term_sync, normalized)

    async def get_custom_safety_terms(self) -> set[str]:
        async with self._lock:
            return await asyncio.to_thread(self._get_custom_safety_terms_sync)

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
        reason: str = "",
        added_by: str = "",
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
                str(reason or "")[:200],
                str(added_by or "")[:100],
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

    # ── 内部同步方法 ──────────────────────────────────────────

    def _init_db(self) -> None:
        conn = self._get_conn()
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
                reason TEXT NOT NULL DEFAULT '',
                added_by TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS content_safety_terms (
                normalized_term TEXT PRIMARY KEY,
                term TEXT NOT NULL,
                added_by TEXT NOT NULL DEFAULT '',
                added_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_page_cursor (
                scope TEXT NOT NULL,
                source_key TEXT NOT NULL,
                page_offset INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope, source_key)
            )
            """
        )
        conn.commit()

    def _cleanup_old_days_sync(self, date_key: str) -> None:
        conn = self._get_conn()
        cursor = conn.execute(
            "DELETE FROM image_usage WHERE date_key != ?", (date_key,)
        )
        conn.commit()
        if cursor.rowcount > 0:
            logger.info(
                f"{LOG_PREFIX} 已清理过期图片去重索引: "
                f"removed={cursor.rowcount}, keep_date={date_key}"
            )

    def _maybe_cleanup_old_days_sync(self, date_key: str) -> None:
        """每小时最多清理一次，避免写操作频繁触发 DELETE。"""
        now_ts = time.monotonic()
        if now_ts - self._last_cleanup_ts < _CLEANUP_THROTTLE_SECONDS:
            return
        self._last_cleanup_ts = now_ts
        self._cleanup_old_days_sync(date_key)

    def _get_page_offset_sync(self, scope: str, source_key: str) -> int:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT page_offset, updated_at FROM source_page_cursor
            WHERE scope = ? AND source_key = ?
            """,
            (scope, source_key),
        ).fetchone()
        if row is None:
            return 0
        page_offset = row["page_offset"]
        updated_at = row["updated_at"]
        # 超过 PAGE_CURSOR_TTL_DAYS 天未访问，重置游标（同一连接）
        try:
            updated = datetime.fromisoformat(updated_at)
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=SHANGHAI_TZ)
            if self._now_dt() - updated > timedelta(days=PAGE_CURSOR_TTL_DAYS):
                conn.execute(
                    "DELETE FROM source_page_cursor WHERE scope = ? AND source_key = ?",
                    (scope, source_key),
                )
                conn.commit()
                return 0
        except (ValueError, TypeError):
            return 0
        return page_offset

    def _advance_page_offset_sync(
        self, scope: str, source_key: str, results_count: int
    ) -> int:
        now = self.now_iso()
        conn = self._get_conn()
        row = conn.execute(
            "SELECT page_offset FROM source_page_cursor WHERE scope = ? AND source_key = ?",
            (scope, source_key),
        ).fetchone()
        current = row["page_offset"] if row is not None else 0

        # 如果结果数明显少于正常页大小，说明已到底，重置为 0
        if results_count < MIN_RESULTS_FOR_NEXT_PAGE:
            new_offset = 0
        else:
            new_offset = current + results_count

        conn.execute(
            """
            INSERT INTO source_page_cursor (scope, source_key, page_offset, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scope, source_key) DO UPDATE SET
                page_offset = excluded.page_offset,
                updated_at = excluded.updated_at
            """,
            (scope, source_key, new_offset, now),
        )
        conn.commit()
        return new_offset

    def _get_used_illust_ids_sync(
        self, date_key: str, scope: str, source_key: str
    ) -> set[str]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT illust_id FROM image_usage
            WHERE date_key = ? AND scope = ? AND source_key = ?
            """,
            (date_key, scope, source_key),
        ).fetchall()
        return {str(row["illust_id"]) for row in rows}

    def _get_blacklisted_illust_ids_sync(self) -> set[str]:
        conn = self._get_conn()
        rows = conn.execute("SELECT illust_id FROM image_blacklist").fetchall()
        return {str(row["illust_id"]) for row in rows}

    def _list_blacklist_illusts_sync(self) -> list[dict[str, Any]]:
        conn = self._get_conn()
        rows = conn.execute(
            """
            SELECT illust_id, title, author, source, record_id, thumb_id,
                   reason, added_by, added_at
            FROM image_blacklist
            ORDER BY added_at DESC, illust_id DESC
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def _list_safety_terms_sync(self) -> list[dict[str, str]]:
        rows = (
            self._get_conn()
            .execute(
                "SELECT term, normalized_term, added_by, added_at "
                "FROM content_safety_terms ORDER BY added_at DESC, term"
            )
            .fetchall()
        )
        return [dict(row) for row in rows]

    def _get_custom_safety_terms_sync(self) -> set[str]:
        rows = (
            self._get_conn()
            .execute("SELECT normalized_term FROM content_safety_terms")
            .fetchall()
        )
        return {str(row["normalized_term"]) for row in rows}

    def _add_safety_term_sync(
        self, term: str, normalized: str, added_by: str, added_at: str
    ) -> bool:
        cursor = self._get_conn().execute(
            """
            INSERT INTO content_safety_terms (normalized_term, term, added_by, added_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(normalized_term) DO UPDATE SET term = excluded.term
            """,
            (normalized, term, added_by, added_at),
        )
        self._get_conn().commit()
        return cursor.rowcount > 0

    def _remove_safety_term_sync(self, normalized: str) -> bool:
        cursor = self._get_conn().execute(
            "DELETE FROM content_safety_terms WHERE normalized_term = ?",
            (normalized,),
        )
        self._get_conn().commit()
        return cursor.rowcount > 0

    def _is_blacklisted_sync(self, illust_id: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM image_blacklist WHERE illust_id = ? LIMIT 1",
            (illust_id,),
        ).fetchone()
        return row is not None

    def _get_blacklist_thumbnail_path_sync(self, illust_id: str) -> Path | None:
        conn = self._get_conn()
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
        conn = self._get_conn()
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
        try:
            shutil.copyfile(source_path, tmp_path)
            tmp_path.replace(dest_path)
        except Exception:
            # 写入失败时清理临时文件，避免残留
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return thumb_id

    def _add_blacklist_illust_sync(
        self,
        illust_id: str,
        title: str,
        author: str,
        source: str,
        record_id: str,
        thumb_id: str,
        reason: str,
        added_by: str,
        added_at: str,
    ) -> bool:
        conn = self._get_conn()
        cursor = conn.execute(
            """
            INSERT INTO image_blacklist (
                illust_id, title, author, source, record_id, thumb_id,
                reason, added_by, added_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(illust_id)
            DO UPDATE SET
                title = CASE
                    WHEN excluded.title != '' THEN excluded.title
                    ELSE image_blacklist.title
                END,
                author = CASE
                    WHEN excluded.author != '' THEN excluded.author
                    ELSE image_blacklist.author
                END,
                source = CASE
                    WHEN excluded.source != '' THEN excluded.source
                    ELSE image_blacklist.source
                END,
                record_id = CASE
                    WHEN excluded.record_id != '' THEN excluded.record_id
                    ELSE image_blacklist.record_id
                END,
                thumb_id = CASE
                    WHEN excluded.thumb_id != '' THEN excluded.thumb_id
                    ELSE image_blacklist.thumb_id
                END,
                reason = CASE
                    WHEN excluded.reason != '' THEN excluded.reason
                    ELSE image_blacklist.reason
                END,
                added_by = CASE
                    WHEN excluded.added_by != '' THEN excluded.added_by
                    ELSE image_blacklist.added_by
                END,
                added_at = excluded.added_at
            """,
            (
                illust_id,
                title,
                author,
                source,
                record_id,
                thumb_id,
                reason,
                added_by,
                added_at,
            ),
        )
        conn.commit()
        return cursor.rowcount > 0

    def _remove_blacklist_illust_sync(self, illust_id: str) -> dict[str, Any] | None:
        conn = self._get_conn()
        row = conn.execute(
            """
            SELECT illust_id, title, author, source, record_id, thumb_id,
                   reason, added_by, added_at
            FROM image_blacklist
            WHERE illust_id = ?
            """,
            (illust_id,),
        ).fetchone()
        if row is None:
            return None
        record = dict(row)
        conn.execute("DELETE FROM image_blacklist WHERE illust_id = ?", (illust_id,))
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
        self._maybe_cleanup_old_days_sync(date_key)
        conn = self._get_conn()
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
        self._maybe_cleanup_old_days_sync(date_key)
        conn = self._get_conn()
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
        self._maybe_cleanup_old_days_sync(date_key)
        conn = self._get_conn()
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
