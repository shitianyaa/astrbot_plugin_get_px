from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
from typing import Any
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
            records = [
                r for r in records if self._record_dedupe_key(r) != dedupe_key
            ]
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
            return count

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

    def _load_records(self) -> list[dict[str, Any]]:
        if not self._history_path.exists():
            return []
        try:
            data = json.loads(self._history_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"{LOG_PREFIX} 读取图片历史失败: {e}")
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

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
        for record in records[self._limit :]:
            thumb_id = str(record.get("thumb_id") or "")
            if thumb_id:
                self._safe_unlink(self._thumb_dir / thumb_id)
        return records[: self._limit]

    def _cleanup_orphan_thumbnails(self, records: list[dict[str, Any]]) -> None:
        valid = {str(record.get("thumb_id") or "") for record in records}
        for path in self._thumb_dir.glob("*.jpg"):
            if path.name not in valid:
                self._safe_unlink(path)

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
            pixiv_url=f"https://www.pixiv.net/artworks/{illust_id}" if illust_id else "",
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
