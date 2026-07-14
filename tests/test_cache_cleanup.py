from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cache_cleanup import cleanup_legacy_caches


def test_cleanup_removes_only_allowlisted_cache_directories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        history = root / "image_history"
        cards = root / "checkin_card_cache"
        history.mkdir()
        cards.mkdir()
        (history / "image_history.json").write_text("[]", encoding="utf-8")
        (cards / "card.jpg").write_bytes(b"cache")
        database = root / "checkin.sqlite3"
        database.write_bytes(b"data")
        blacklist = root / "image_blacklist"
        blacklist.mkdir()
        (blacklist / "keep.jpg").write_bytes(b"keep")

        summary = cleanup_legacy_caches(root)

        assert summary.cleaned == 1
        assert summary.files == 1
        assert not history.exists()
        # checkin_card_cache 是当天签到卡片 JPEG 缓存，由 CheckinCardCleanup 自身按日过期，
        # 启动时清整目录会抹掉当天 warm cache，因此不在遗留清理范围。
        assert cards.exists()
        assert (cards / "card.jpg").read_bytes() == b"cache"
        assert database.read_bytes() == b"data"
        assert (blacklist / "keep.jpg").read_bytes() == b"keep"


def test_cleanup_skips_missing_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        summary = cleanup_legacy_caches(tmp)
        assert summary.cleaned == 0
        assert summary.skipped == 1
        assert summary.failed == 0


def test_cleanup_unlinks_cache_symlink_without_deleting_target() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        backups = root / "checkin_backups"
        backups.mkdir()
        backup = backups / "keep.json"
        backup.write_text("{}", encoding="utf-8")
        cache_link = root / "image_history"
        try:
            cache_link.symlink_to(backups, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"当前环境不能创建目录符号链接: {exc}")

        summary = cleanup_legacy_caches(root)

        assert summary.cleaned == 1
        assert not cache_link.exists()
        assert backup.read_text(encoding="utf-8") == "{}"
