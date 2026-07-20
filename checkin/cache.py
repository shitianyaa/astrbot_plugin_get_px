from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from astrbot.api.all import logger
from PIL import Image


LOG_PREFIX = "[GetPx]"
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_CACHE_KEY_PATTERN = re.compile(r"[0-9a-f]{64}")
_DEFAULT_EXPECTED_SIZE = (960, 540)

Renderer = Callable[[], str | Path | Awaitable[str | Path]]


class CheckinCardCache:
    """One-day derived JPEG cache for rendered check-in cards."""

    def __init__(self, root: Path, retention_days: int = 1):
        if retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.retention_days = int(retention_days)
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_cleanup_date: date | None = None
        self._active_store_lock = threading.Lock()
        self._active_store_dates: dict[str, int] = {}

    @staticmethod
    def cache_key(
        *,
        date_key: str,
        user_id: str,
        template_version: str,
        view_model: Any,
    ) -> str:
        normalized_date = _validated_date_key(date_key)
        view_model_json = json.dumps(
            view_model,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        view_model_digest = hashlib.sha256(view_model_json.encode("utf-8")).hexdigest()
        identity = json.dumps(
            {
                "date_key": normalized_date,
                "template_version": str(template_version or ""),
                "user_id": str(user_id or ""),
                "view_model_digest": view_model_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def get(
        self,
        date_key: str,
        key: str,
        *,
        expected_size: tuple[int, int] = _DEFAULT_EXPECTED_SIZE,
    ) -> Path | None:
        """Return a validated cache hit, removing corrupt derived files."""
        path = self._cache_path(date_key, key)
        self.cleanup_expired()
        if not path.is_file():
            return None
        rejection_reason = _card_jpeg_rejection_reason(path, expected_size)
        if rejection_reason is None:
            return path
        width, height = expected_size
        logger.warning(
            f"{LOG_PREFIX} 签到卡缓存拒绝: reason={rejection_reason} "
            f"date={date_key} expected_size={width}x{height}"
        )
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                f"{LOG_PREFIX} 签到卡缓存清理失败: stage=remove_rejected "
                f"error_type={type(exc).__name__}"
            )
        return None

    async def store(
        self,
        date_key: str,
        key: str,
        renderer: Renderer,
        *,
        expected_size: tuple[int, int] = _DEFAULT_EXPECTED_SIZE,
    ) -> Path:
        """Render at most once for a key and atomically publish the result."""
        final_path = self._cache_path(date_key, key)
        with self._active_store_lock:
            lock = self._locks.setdefault(f"{date_key}:{key}", asyncio.Lock())
            self._active_store_dates[date_key] = (
                self._active_store_dates.get(date_key, 0) + 1
            )
        try:
            async with lock:
                cached = await asyncio.to_thread(
                    self.get,
                    date_key,
                    key,
                    expected_size=expected_size,
                )
                if cached is not None:
                    logger.debug(
                        f"{LOG_PREFIX} 签到卡缓存并发命中: date={date_key} "
                        f"expected_size={expected_size[0]}x{expected_size[1]}"
                    )
                    return cached

                rendered = renderer()
                source = await rendered if inspect.isawaitable(rendered) else rendered
                worker = asyncio.create_task(
                    asyncio.to_thread(
                        self._store_sync,
                        Path(source),
                        final_path,
                        expected_size,
                    )
                )
                cancelled = False
                while not worker.done():
                    try:
                        result = await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        cancelled = True
                    else:
                        break

                if cancelled:
                    try:
                        worker.result()
                    except Exception:
                        pass
                    raise asyncio.CancelledError
                if worker.done():
                    stored = worker.result()
                else:
                    stored = result
                logger.debug(
                    f"{LOG_PREFIX} 签到卡缓存写入完成: date={date_key} "
                    f"expected_size={expected_size[0]}x{expected_size[1]}"
                )
                return stored
        finally:
            self._end_store(date_key)

    def cleanup_expired(
        self,
        *,
        today: date | None = None,
        force: bool = False,
    ) -> int:
        """Remove expired date directories and temporary files once per day."""
        current = today or datetime.now(SHANGHAI_TZ).date()
        if not force and self._last_cleanup_date == current:
            return 0

        removed = 0
        cleanup_incomplete = False
        cutoff = current - timedelta(days=self.retention_days - 1)
        with self._active_store_lock:
            deferred_expired_store = any(
                active_date is not None and active_date < cutoff
                for active_date in (
                    _directory_date(date_key) for date_key in self._active_store_dates
                )
            )
            try:
                self.root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到卡缓存清理失败: stage=prepare_root "
                    f"error_type={type(exc).__name__}"
                )
                return 0
            try:
                entries = tuple(self.root.iterdir())
            except OSError as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到卡缓存清理失败: stage=list_root "
                    f"error_type={type(exc).__name__}"
                )
                return 0
            for entry in entries:
                try:
                    if entry.is_file() or entry.is_symlink():
                        if entry.name.endswith(".tmp"):
                            entry.unlink(missing_ok=True)
                            removed += 1
                        elif entry.is_symlink():
                            entry_date = _directory_date(entry.name)
                            if entry_date is not None and entry_date < cutoff:
                                entry.unlink(missing_ok=True)
                                removed += 1
                        continue

                    if not entry.is_dir():
                        continue
                    entry_date = _directory_date(entry.name)
                    if entry_date is None:
                        continue
                    if entry.name in self._active_store_dates:
                        if entry_date < cutoff:
                            deferred_expired_store = True
                        continue
                    try:
                        resolved = _inside(entry, self.root)
                    except ValueError:
                        cleanup_incomplete = True
                        logger.warning(
                            f"{LOG_PREFIX} 签到卡缓存清理跳过越界目录"
                        )
                        continue
                    if entry_date < cutoff:
                        shutil.rmtree(resolved)
                        removed += 1
                        continue
                    for temporary in resolved.glob("*.tmp"):
                        if temporary.is_file() or temporary.is_symlink():
                            temporary.unlink(missing_ok=True)
                            removed += 1
                except OSError as exc:
                    cleanup_incomplete = True
                    logger.warning(
                        f"{LOG_PREFIX} 签到卡缓存清理失败: stage=remove_entry "
                        f"error_type={type(exc).__name__}"
                    )

            self._locks = {
                lock_key: lock
                for lock_key, lock in self._locks.items()
                if lock.locked()
                or (lock_date := _directory_date(lock_key.split(":", 1)[0])) is None
                or lock_date >= cutoff
            }

        if removed:
            logger.debug(
                f"{LOG_PREFIX} 签到卡缓存清理完成: removed={removed} "
                f"cutoff={cutoff.isoformat()}"
            )
        if not deferred_expired_store and not cleanup_incomplete:
            self._last_cleanup_date = current
        return removed

    def _end_store(self, date_key: str) -> None:
        with self._active_store_lock:
            remaining = self._active_store_dates.get(date_key, 0) - 1
            if remaining > 0:
                self._active_store_dates[date_key] = remaining
            else:
                self._active_store_dates.pop(date_key, None)

    def _cache_path(self, date_key: str, key: str) -> Path:
        normalized_date = _validated_date_key(date_key)
        normalized_key = str(key or "")
        if _CACHE_KEY_PATTERN.fullmatch(normalized_key) is None:
            raise ValueError("cache key must be a lowercase SHA-256 digest")
        return _inside(
            self.root / normalized_date / f"{normalized_key}.jpg",
            self.root,
        )

    @staticmethod
    def _store_sync(
        source: Path,
        final_path: Path,
        expected_size: tuple[int, int],
    ) -> Path:
        if not source.is_file():
            raise ValueError("renderer did not produce a file")
        final_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with (
                source.open("rb") as source_file,
                tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=final_path.parent,
                    prefix=f".{final_path.stem}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary_file,
            ):
                temporary_path = Path(temporary_file.name)
                shutil.copyfileobj(source_file, temporary_file)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            if not is_valid_card_jpeg(temporary_path, expected_size):
                width, height = expected_size
                raise ValueError(
                    f"renderer output must be a valid {width}x{height} JPEG"
                )
            os.replace(temporary_path, final_path)
            temporary_path = None
            return final_path.resolve()
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        f"{LOG_PREFIX} 签到卡缓存清理失败: "
                        f"stage=remove_temporary error_type={type(exc).__name__}"
                    )


def _validated_date_key(value: str) -> str:
    raw = str(value or "")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("date_key must use YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise ValueError("date_key must use YYYY-MM-DD")
    return raw


def _directory_date(name: str) -> date | None:
    try:
        parsed = date.fromisoformat(name)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == name else None


def _inside(path: Path, root: Path) -> Path:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("cache path escapes its root") from exc
    return resolved_path


def is_valid_card_jpeg(path: Path, expected_size: tuple[int, int]) -> bool:
    return _card_jpeg_rejection_reason(path, expected_size) is None


def _card_jpeg_rejection_reason(
    path: Path, expected_size: tuple[int, int]
) -> str | None:
    try:
        with Image.open(path) as image:
            if image.format != "JPEG":
                return "format_mismatch"
            if image.size != expected_size:
                return "size_mismatch"
            image.verify()
        return None
    except (OSError, ValueError):
        return "corrupt_or_unreadable"


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    raise TypeError(f"unsupported cache key value: {type(value).__name__}")
