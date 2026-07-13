from __future__ import annotations

import tempfile
from pathlib import Path

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

        assert summary.cleaned == 2
        assert summary.files == 2
        assert not history.exists()
        assert not cards.exists()
        assert database.read_bytes() == b"data"
        assert (blacklist / "keep.jpg").read_bytes() == b"keep"


def test_cleanup_skips_missing_targets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        summary = cleanup_legacy_caches(tmp)
        assert summary.cleaned == 0
        assert summary.skipped == 2
        assert summary.failed == 0
