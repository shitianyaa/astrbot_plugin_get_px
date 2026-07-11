import asyncio
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin_cache import CheckinCardCache  # noqa: E402


def _write_jpeg(path: Path, *, size: tuple[int, int] = (960, 540)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (238, 224, 196)).save(path, format="JPEG")


class CheckinCardCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "checkin_card_cache"
        self.cache = CheckinCardCache(self.root)
        self.date_key = "2026-07-11"
        self.key = self.cache.cache_key(
            date_key=self.date_key,
            user_id="10001",
            template_version="v2",
            view_model={"greeting": "今天也见面了", "badges": ["夏日", "连签5天"]},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_key_is_stable_and_changes_with_each_identity_component(self):
        kwargs = {
            "date_key": self.date_key,
            "user_id": "10001",
            "template_version": "v2",
            "view_model": {"badges": ["夏日"], "greeting": "你好"},
        }
        first = self.cache.cache_key(**kwargs)
        reordered = self.cache.cache_key(
            **{**kwargs, "view_model": {"greeting": "你好", "badges": ["夏日"]}}
        )

        self.assertEqual(first, reordered)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        for field, changed in (
            ("date_key", "2026-07-12"),
            ("user_id", "10002"),
            ("template_version", "v3"),
            ("view_model", {"badges": ["夏日"], "greeting": "再见"}),
        ):
            self.assertNotEqual(first, self.cache.cache_key(**{**kwargs, field: changed}))

    def test_get_returns_only_valid_960_by_540_jpeg(self):
        expected = self.root / self.date_key / f"{self.key}.jpg"
        _write_jpeg(expected)

        self.assertEqual(self.cache.get(self.date_key, self.key), expected.resolve())

    def test_get_removes_corrupt_or_wrong_sized_cache_files(self):
        corrupt = self.root / self.date_key / f"{self.key}.jpg"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a jpeg")

        self.assertIsNone(self.cache.get(self.date_key, self.key))
        self.assertFalse(corrupt.exists())

        _write_jpeg(corrupt, size=(320, 180))
        self.assertIsNone(self.cache.get(self.date_key, self.key))
        self.assertFalse(corrupt.exists())

    async def test_store_copies_valid_renderer_output_atomically(self):
        renderer_output = Path(self._tmp.name) / "renderer-output.jpg"
        _write_jpeg(renderer_output)

        async def render() -> str:
            return str(renderer_output)

        stored = await self.cache.store(self.date_key, self.key, render)

        self.assertEqual(stored, (self.root / self.date_key / f"{self.key}.jpg").resolve())
        self.assertTrue(renderer_output.exists())
        self.assertEqual(self.cache.get(self.date_key, self.key), stored)
        self.assertEqual(list(stored.parent.glob("*.tmp")), [])

    async def test_store_uses_one_renderer_for_concurrent_same_key_requests(self):
        renderer_output = Path(self._tmp.name) / "renderer-output.jpg"
        _write_jpeg(renderer_output)
        render_count = 0

        async def render() -> str:
            nonlocal render_count
            render_count += 1
            await asyncio.sleep(0.01)
            return str(renderer_output)

        first, second = await asyncio.gather(
            self.cache.store(self.date_key, self.key, render),
            self.cache.store(self.date_key, self.key, render),
        )

        self.assertEqual(first, second)
        self.assertEqual(render_count, 1)

    async def test_cross_day_cleanup_does_not_remove_an_active_store_temp(self):
        renderer_output = Path(self._tmp.name) / "renderer-output.jpg"
        _write_jpeg(renderer_output)
        copy_started = threading.Event()
        finish_copy = threading.Event()
        original_copyfileobj = shutil.copyfileobj

        def slow_copy(source, target, *args, **kwargs):
            copy_started.set()
            if not finish_copy.wait(timeout=2):
                raise TimeoutError("test did not release cache copy")
            return original_copyfileobj(source, target, *args, **kwargs)

        async def render() -> str:
            return str(renderer_output)

        with patch(
            "astrbot_plugin_get_px.checkin_cache.shutil.copyfileobj",
            side_effect=slow_copy,
        ):
            store_task = asyncio.create_task(
                self.cache.store(self.date_key, self.key, render)
            )
            self.assertTrue(await asyncio.to_thread(copy_started.wait, 2))
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    self.cache.cleanup_expired,
                    today=date(2026, 7, 12),
                    force=True,
                )
            )
            await asyncio.sleep(0.02)
            finish_copy.set()
            cleanup_result, store_result = await asyncio.gather(
                cleanup_task,
                store_task,
                return_exceptions=True,
            )

        self.assertIsInstance(cleanup_result, int)
        self.assertIsInstance(store_result, Path)
        self.assertTrue(store_result.exists())
        self.assertEqual(self.cache.get(self.date_key, self.key), store_result)

    def test_cleanup_removes_previous_days_and_stale_temps_but_preserves_today(self):
        yesterday = self.root / "2026-07-10"
        today = self.root / self.date_key
        _write_jpeg(yesterday / "old.jpg")
        _write_jpeg(today / "current.jpg")
        stale_tmp = today / ".partial.tmp"
        stale_tmp.write_bytes(b"partial")

        removed = self.cache.cleanup_expired(today=date(2026, 7, 11), force=True)

        self.assertGreaterEqual(removed, 2)
        self.assertFalse(yesterday.exists())
        self.assertTrue((today / "current.jpg").exists())
        self.assertFalse(stale_tmp.exists())

    def test_cleanup_runs_at_most_once_for_the_same_day(self):
        self.cache.cleanup_expired(today=date(2026, 7, 11))
        late_old_dir = self.root / "2026-07-10"
        _write_jpeg(late_old_dir / "old.jpg")

        self.assertEqual(self.cache.cleanup_expired(today=date(2026, 7, 11)), 0)
        self.assertTrue(late_old_dir.exists())

    def test_invalid_date_or_key_cannot_escape_cache_root(self):
        for date_key, key in (
            ("../outside", self.key),
            (self.date_key, "../outside"),
            ("2026-7-11", self.key),
            (self.date_key, "not-a-sha256"),
        ):
            with self.subTest(date_key=date_key, key=key):
                with self.assertRaises(ValueError):
                    self.cache.get(date_key, key)

        self.assertFalse((self.root.parent / "outside.jpg").exists())


if __name__ == "__main__":
    unittest.main()
