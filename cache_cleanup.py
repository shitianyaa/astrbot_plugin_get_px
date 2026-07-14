from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

from astrbot.api import logger


LOG_PREFIX = "[GetPx]"
LEGACY_CACHE_TARGETS = ("image_history", "checkin_card_cache")


@dataclass(frozen=True)
class CacheCleanupSummary:
    cleaned: int = 0
    skipped: int = 0
    failed: int = 0
    files: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "cleaned": self.cleaned,
            "skipped": self.skipped,
            "failed": self.failed,
            "files": self.files,
            "bytes": self.bytes,
        }


def cleanup_legacy_caches(data_dir: Path | str) -> CacheCleanupSummary:
    root = Path(data_dir).expanduser().resolve()
    logger.info(f"{LOG_PREFIX} 开始清理旧版缓存")
    cleaned = skipped = failed = files = total_bytes = 0

    for name in LEGACY_CACHE_TARGETS:
        try:
            raw_target = root / name
            if _is_link(raw_target):
                _remove_link(raw_target)
                cleaned += 1
                logger.info(f"{LOG_PREFIX} 缓存链接已清理: target={name}")
                continue
            target = raw_target.resolve()
            target.relative_to(root)
            if target == root:
                raise ValueError("cache target resolves to data root")
            if not target.exists() and not target.is_symlink():
                skipped += 1
                logger.info(f"{LOG_PREFIX} 缓存不存在，跳过: target={name}")
                continue
            item_files, item_bytes = _measure_tree(target)
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            cleaned += 1
            files += item_files
            total_bytes += item_bytes
            logger.info(
                f"{LOG_PREFIX} 缓存已清理: target={name} "
                f"files={item_files} bytes={item_bytes}"
            )
        except Exception as exc:
            failed += 1
            logger.warning(
                f"{LOG_PREFIX} 缓存清理失败: target={name} error={type(exc).__name__}"
            )

    summary = CacheCleanupSummary(cleaned, skipped, failed, files, total_bytes)
    logger.info(
        f"{LOG_PREFIX} 旧版缓存清理完成: cleaned={cleaned} skipped={skipped} "
        f"failed={failed} files={files} bytes={total_bytes}"
    )
    return summary


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(callable(is_junction) and is_junction())


def _remove_link(path: Path) -> None:
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction() and not path.is_symlink():
        path.rmdir()
    else:
        path.unlink(missing_ok=True)


def _measure_tree(path: Path) -> tuple[int, int]:
    if path.is_file() or path.is_symlink():
        try:
            return 1, path.stat().st_size
        except OSError:
            return 1, 0
    files = 0
    total_bytes = 0
    for item in path.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        files += 1
        try:
            total_bytes += item.stat().st_size
        except OSError:
            pass
    return files, total_bytes
