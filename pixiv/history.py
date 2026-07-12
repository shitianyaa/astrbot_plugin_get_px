from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any, Iterable
from uuid import uuid4

from astrbot.api import logger

LOG_PREFIX = "[GetPx]"
DEFAULT_HISTORY_LIMIT = 200
THUMBNAIL_SIZE = 360
THUMBNAIL_QUALITY = 82


@dataclass
class ImageHistoryRecord:
    record_id: str
    sent_at: str
    source: str
    illust_id: str
    page: int
    title: str
    author: str
    author_id: str
    x_restrict: int
    width: int
    height: int
    page_count: int
    tags: list[str]
    pixiv_url: str
    thumb_id: str
    session_id: str
    group_id: str
    sender_id: str
    platform: str
    quality: str
    file_size: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ImageAssetManager:
    def __init__(self, data_dir: Path | str, limit: int = DEFAULT_HISTORY_LIMIT):
        self._root = Path(data_dir) / "image_history"
        self._thumb_dir = self._root / "thumbs"
        self._history_path = self._root / "image_history.json"
        self._limit = max(1, limit)
        self._lock = asyncio.Lock()
        self._root.mkdir(parents=True, exist_ok=True)
        self._thumb_dir.mkdir(parents=True, exist_ok=True)

    async def record_sent(
        self,
        *,
        illust: dict,
        image_path: str,
        event,
        source: str,
        page: int = 1,
        quality: str = "",
        file_size: int = 0,
    ) -> None:
        if not image_path or not os.path.exists(image_path):
            return

        async with self._lock:
            records = self._load_records()
            record_id = self._make_record_id(illust, page)
            thumb_id = f"{record_id}.jpg"
            thumb_path = self._thumb_dir / thumb_id

            try:
                await asyncio.to_thread(self._create_thumbnail, image_path, thumb_path)
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 生成图片历史缩略图失败: {e}")
                thumb_id = ""

            record = self._build_record(
                record_id=record_id,
                thumb_id=thumb_id,
                illust=illust,
                event=event,
                source=source,
                page=page,
                quality=quality,
                file_size=file_size,
            )
            dedupe_key = self._record_dedupe_key(record.to_dict())
            records = [r for r in records if self._record_dedupe_key(r) != dedupe_key]
            records.insert(0, record.to_dict())
            records = self._trim_records(records)
            self._save_records(records)
            self._cleanup_orphan_thumbnails(records)

    async def list_records(self) -> list[dict[str, Any]]:
        async with self._lock:
            records = self._load_records()
            deduped = self._deduplicate_records(records)
            if len(deduped) != len(records):
                self._save_records(deduped)
                self._cleanup_orphan_thumbnails(deduped)
            return deduped

    async def clear(self) -> int:
        async with self._lock:
            count = len(self._load_records())
            self._save_records([])
            if self._thumb_dir.exists():
                shutil.rmtree(self._thumb_dir, ignore_errors=True)
            self._thumb_dir.mkdir(parents=True, exist_ok=True)
            logger.info(
                f"{LOG_PREFIX} 已清空图片历史: records={count}, "
                f"thumb_dir={self._thumb_dir}"
            )
            return count

    async def get_record(self, record_id: str) -> dict[str, Any] | None:
        if not record_id:
            return None
        async with self._lock:
            for record in self._load_records():
                if str(record.get("record_id") or "") == record_id:
                    return dict(record)
        return None

    async def delete_record(self, record_id: str) -> dict[str, Any] | None:
        if not record_id:
            return None
        async with self._lock:
            deleted = self._delete_matching_records(
                lambda record: str(record.get("record_id") or "") == record_id
            )
        return deleted[0] if deleted else None

    async def delete_records_by_illust_id(self, illust_id: str) -> list[dict[str, Any]]:
        illust_id = str(illust_id or "")
        if not illust_id:
            return []
        async with self._lock:
            return self._delete_matching_records(
                lambda record: str(record.get("illust_id") or "") == illust_id
            )

    async def get_thumbnail_path(self, record_id: str) -> Path | None:
        if not record_id:
            return None
        async with self._lock:
            for record in self._load_records():
                if record.get("record_id") == record_id:
                    thumb_id = str(record.get("thumb_id") or "")
                    if not thumb_id:
                        return None
                    path = (self._thumb_dir / thumb_id).resolve()
                    if self._thumb_dir.resolve() not in path.parents:
                        return None
                    return path if path.exists() else None
        return None

    async def get_thumbnail_paths(self, record_ids: Iterable[str]) -> dict[str, Path]:
        ids = self._normalize_ids(record_ids)
        if not ids:
            return {}
        wanted = set(ids)
        async with self._lock:
            thumb_dir = self._thumb_dir.resolve()
            paths: dict[str, Path] = {}
            for record in self._load_records():
                record_id = str(record.get("record_id") or "")
                if record_id not in wanted or record_id in paths:
                    continue
                thumb_id = str(record.get("thumb_id") or "")
                if not thumb_id:
                    continue
                path = (self._thumb_dir / thumb_id).resolve()
                if thumb_dir not in path.parents:
                    continue
                if path.exists():
                    paths[record_id] = path
        return paths

    def _load_records(self) -> list[dict[str, Any]]:
        if not self._history_path.exists():
            return []
        try:
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"{LOG_PREFIX} 读取图片历史失败: {e}")
            self._backup_corrupt_history(e)
            return []
        if not isinstance(data, list):
            logger.warning(f"{LOG_PREFIX} 图片历史格式无效，已按损坏文件处理")
            self._backup_corrupt_history(ValueError("history root is not a list"))
            return []
        return [item for item in data if isinstance(item, dict)]

    def _backup_corrupt_history(self, reason: object) -> None:
        if not self._history_path.exists():
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        backup_path = self._history_path.with_name(
            f"{self._history_path.name}.corrupt.{timestamp}"
        )
        index = 1
        while backup_path.exists():
            backup_path = self._history_path.with_name(
                f"{self._history_path.name}.corrupt.{timestamp}.{index}"
            )
            index += 1
        try:
            self._history_path.replace(backup_path)
            logger.warning(
                f"{LOG_PREFIX} 已备份损坏的图片历史文件: {backup_path} ({reason})"
            )
            # 只保留最近 5 个损坏备份
            self._cleanup_corrupt_backups()
        except OSError as e:
            logger.warning(f"{LOG_PREFIX} 备份损坏图片历史失败: {e}")

    def _cleanup_corrupt_backups(self) -> None:
        """保留最近 5 个 .corrupt.* 备份文件，删除更早的。"""
        parent = self._history_path.parent
        base = self._history_path.name
        corrupt_files = sorted(
            parent.glob(f"{base}.corrupt.*"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for f in corrupt_files[5:]:
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass

    def _delete_matching_records(self, predicate) -> list[dict[str, Any]]:
        records = self._load_records()
        kept: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        for record in records:
            if predicate(record):
                deleted.append(record)
            else:
                kept.append(record)
        if not deleted:
            return []
        for record in deleted:
            thumb_id = str(record.get("thumb_id") or "")
            if thumb_id:
                self._safe_unlink(self._thumb_dir / thumb_id)
        self._save_records(kept)
        orphan_count = self._cleanup_orphan_thumbnails(kept)
        logger.info(
            f"{LOG_PREFIX} 已删除图片历史记录: records={len(deleted)}, "
            f"orphan_thumbnails={orphan_count}"
        )
        return deleted

    def _save_records(self, records: list[dict[str, Any]]) -> None:
        tmp_path = self._history_path.with_suffix(".json.tmp")
        tmp_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp_path.replace(self._history_path)

    def _trim_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(records) <= self._limit:
            return records
        trimmed_count = len(records) - self._limit
        for record in records[self._limit :]:
            thumb_id = str(record.get("thumb_id") or "")
            if thumb_id:
                self._safe_unlink(self._thumb_dir / thumb_id)
        logger.info(
            f"{LOG_PREFIX} 图片历史超过保留上限，已清理旧记录: "
            f"removed={trimmed_count}, limit={self._limit}"
        )
        return records[: self._limit]

    def _cleanup_orphan_thumbnails(self, records: list[dict[str, Any]]) -> int:
        valid = {str(record.get("thumb_id") or "") for record in records}
        removed = 0
        for path in self._thumb_dir.glob("*.jpg"):
            if path.name not in valid:
                self._safe_unlink(path)
                removed += 1
        if removed:
            logger.info(
                f"{LOG_PREFIX} 已清理图片历史孤儿缩略图: "
                f"removed={removed}, thumb_dir={self._thumb_dir}"
            )
        return removed

    def _deduplicate_records(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        seen: set[tuple[str, str, str, str, str, str, str]] = set()
        deduped: list[dict[str, Any]] = []
        for record in records:
            key = self._record_dedupe_key(record)
            if key in seen:
                thumb_id = str(record.get("thumb_id") or "")
                if thumb_id:
                    self._safe_unlink(self._thumb_dir / thumb_id)
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    @staticmethod
    def _record_dedupe_key(
        record: dict[str, Any],
    ) -> tuple[str, str, str, str, str, str, str]:
        illust_id = str(record.get("illust_id") or "")
        if not illust_id:
            illust_id = str(record.get("record_id") or "")
        return (
            illust_id,
            str(record.get("page") or 1),
            str(record.get("source") or ""),
            str(record.get("session_id") or ""),
            str(record.get("group_id") or ""),
            str(record.get("sender_id") or ""),
            str(record.get("platform") or ""),
        )

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

    @staticmethod
    def _make_record_id(illust: dict, page: int) -> str:
        illust_id = str(illust.get("id") or "unknown")
        return f"{illust_id}_p{max(1, page)}_{uuid4().hex[:12]}"

    def _build_record(
        self,
        *,
        record_id: str,
        thumb_id: str,
        illust: dict,
        event,
        source: str,
        page: int,
        quality: str,
        file_size: int,
    ) -> ImageHistoryRecord:
        user = illust.get("user") or {}
        tags = [
            str(tag.get("name", "")).strip()
            for tag in (illust.get("tags") or [])
            if isinstance(tag, dict) and str(tag.get("name", "")).strip()
        ][:12]
        illust_id = str(illust.get("id") or "")
        group_id = event.get_group_id()
        sender_id = event.get_sender_id()
        return ImageHistoryRecord(
            record_id=record_id,
            sent_at=datetime.now(timezone.utc).isoformat(),
            source=source,
            illust_id=illust_id,
            page=max(1, page),
            title=str(illust.get("title") or "无标题"),
            author=str(user.get("name") or "未知"),
            author_id=str(user.get("id") or ""),
            x_restrict=int(illust.get("x_restrict", 0) or 0),
            width=int(illust.get("width", 0) or 0),
            height=int(illust.get("height", 0) or 0),
            page_count=int(illust.get("page_count", 1) or 1),
            tags=tags,
            pixiv_url=f"https://www.pixiv.net/artworks/{illust_id}"
            if illust_id
            else "",
            thumb_id=thumb_id,
            session_id=str(getattr(event, "unified_msg_origin", "") or ""),
            group_id=str(group_id or ""),
            sender_id=str(sender_id or ""),
            platform=str(event.get_platform_name() or ""),
            quality=quality,
            file_size=int(file_size or 0),
        )

    @staticmethod
    def _create_thumbnail(src_path: str, dst_path: Path) -> None:
        from PIL import Image as PILImage

        dst_path.parent.mkdir(parents=True, exist_ok=True)
        with PILImage.open(src_path) as img:
            img.thumbnail((THUMBNAIL_SIZE, THUMBNAIL_SIZE))
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(
                dst_path,
                format="JPEG",
                quality=THUMBNAIL_QUALITY,
                optimize=True,
            )
