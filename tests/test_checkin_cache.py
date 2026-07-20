import asyncio
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin.cache import CheckinCardCache  # noqa: E402


def _write_jpeg(path: Path, *, size: tuple[int, int] = (960, 540)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (238, 224, 196)).save(path, format="JPEG")


class CheckinCardCacheTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "checkin_card_cache"
        self.cache = CheckinCardCache(self.root)
        self.date_key = date.today().isoformat()
        self.next_day = date.fromisoformat(self.date_key) + timedelta(days=1)
        self.key = self.cache.cache_key(
            date_key=self.date_key,
            user_id="10001",
            template_version="default:1",
            view_model={"greeting": "今天也见面了", "badges": ["夏日", "连签5天"]},
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_key_is_stable_and_changes_with_each_identity_component(self):
        kwargs = {
            "date_key": self.date_key,
            "user_id": "10001",
            "template_version": "default:1",
            "view_model": {"badges": ["夏日"], "greeting": "你好"},
        }
        first = self.cache.cache_key(**kwargs)
        reordered = self.cache.cache_key(
            **{**kwargs, "view_model": {"greeting": "你好", "badges": ["夏日"]}}
        )

        self.assertEqual(first, reordered)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        for field, changed in (
            ("date_key", self.next_day.isoformat()),
            ("user_id", "10002"),
            ("template_version", "v3"),
            ("view_model", {"badges": ["夏日"], "greeting": "再见"}),
        ):
            self.assertNotEqual(
                first, self.cache.cache_key(**{**kwargs, field: changed})
            )

    def test_get_returns_only_valid_960_by_540_jpeg(self):
        expected = self.root / self.date_key / f"{self.key}.jpg"
        _write_jpeg(expected)

        self.assertEqual(self.cache.get(self.date_key, self.key), expected.resolve())

    def test_get_removes_corrupt_or_wrong_sized_cache_files(self):
        corrupt = self.root / self.date_key / f"{self.key}.jpg"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a jpeg")

        with patch("astrbot_plugin_get_px.checkin.cache.logger") as mock_logger:
            self.assertIsNone(self.cache.get(self.date_key, self.key))
        self.assertIn(
            "原因=corrupt_or_unreadable",
            " ".join(str(call) for call in mock_logger.warning.call_args_list),
        )
        self.assertFalse(corrupt.exists())

        _write_jpeg(corrupt, size=(320, 180))
        with patch("astrbot_plugin_get_px.checkin.cache.logger") as mock_logger:
            self.assertIsNone(self.cache.get(self.date_key, self.key))
        self.assertIn(
            "原因=size_mismatch",
            " ".join(str(call) for call in mock_logger.warning.call_args_list),
        )
        self.assertFalse(corrupt.exists())

    def test_cleanup_failure_does_not_block_a_current_cache_hit(self):
        current = self.root / self.date_key / f"{self.key}.jpg"
        _write_jpeg(current)
        old_date = (date.fromisoformat(self.date_key) - timedelta(days=2)).isoformat()
        (self.root / old_date).mkdir(parents=True)

        with (
            patch(
                "astrbot_plugin_get_px.checkin.cache.shutil.rmtree",
                side_effect=PermissionError(r"C:\secret\locked"),
            ),
            patch("astrbot_plugin_get_px.checkin.cache.logger") as mock_logger,
        ):
            cached = self.cache.get(self.date_key, self.key)

        self.assertEqual(cached, current.resolve())
        messages = " ".join(str(call) for call in mock_logger.warning.call_args_list)
        self.assertIn("阶段=删除缓存条目", messages)
        self.assertIn("错误类型=PermissionError", messages)
        self.assertNotIn("secret", messages)

        self.assertEqual(
            self.cache.cleanup_expired(today=date.fromisoformat(self.date_key)), 1
        )
        self.assertFalse((self.root / old_date).exists())

    def test_rejected_cache_delete_failure_does_not_block_miss(self):
        corrupt = self.root / self.date_key / f"{self.key}.jpg"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_bytes(b"not a jpeg")

        with (
            patch.object(Path, "unlink", side_effect=PermissionError("private path")),
            patch("astrbot_plugin_get_px.checkin.cache.logger") as mock_logger,
        ):
            cached = self.cache.get(self.date_key, self.key)

        self.assertIsNone(cached)
        messages = " ".join(str(call) for call in mock_logger.warning.call_args_list)
        self.assertIn("阶段=删除拒绝文件", messages)
        self.assertIn("错误类型=PermissionError", messages)
        self.assertNotIn("private path", messages)

    def test_expected_size_is_validated_per_render_tier(self):
        cached = self.root / self.date_key / f"{self.key}.jpg"
        _write_jpeg(cached, size=(1248, 702))

        self.assertEqual(
            self.cache.get(
                self.date_key,
                self.key,
                expected_size=(1248, 702),
            ),
            cached.resolve(),
        )
        self.assertIsNone(
            self.cache.get(
                self.date_key,
                self.key,
                expected_size=(1728, 972),
            )
        )
        self.assertFalse(cached.exists())

    async def test_store_accepts_exact_non_default_tier_size(self):
        renderer_output = Path(self._tmp.name) / "clear-output.jpg"
        _write_jpeg(renderer_output, size=(1248, 702))

        stored = await self.cache.store(
            self.date_key,
            self.key,
            lambda: str(renderer_output),
            expected_size=(1248, 702),
        )

        self.assertEqual(
            self.cache.get(
                self.date_key,
                self.key,
                expected_size=(1248, 702),
            ),
            stored,
        )

    async def test_store_copies_valid_renderer_output_atomically(self):
        renderer_output = Path(self._tmp.name) / "renderer-output.jpg"
        _write_jpeg(renderer_output)

        async def render() -> str:
            return str(renderer_output)

        stored = await self.cache.store(self.date_key, self.key, render)

        self.assertEqual(
            stored, (self.root / self.date_key / f"{self.key}.jpg").resolve()
        )
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
            "astrbot_plugin_get_px.checkin.cache.shutil.copyfileobj",
            side_effect=slow_copy,
        ):
            store_task = asyncio.create_task(
                self.cache.store(self.date_key, self.key, render)
            )
            self.assertTrue(await asyncio.to_thread(copy_started.wait, 2))
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    self.cache.cleanup_expired,
                    today=self.next_day,
                    force=True,
                )
            )
            cleanup_result = await cleanup_task
            finish_copy.set()
            store_result = await store_task

        self.assertIsInstance(cleanup_result, int)
        self.assertIsInstance(store_result, Path)
        self.assertTrue(store_result.exists())
        self.assertEqual(self.cache.get(self.date_key, self.key), store_result)

    async def test_repeatedly_cancelled_store_waits_for_copy_before_cleanup_and_source_release(
        self,
    ):
        renderer_output = Path(self._tmp.name) / "renderer-output.jpg"
        _write_jpeg(renderer_output)
        copy_started = threading.Event()
        finish_copy = threading.Event()
        copy_finished = threading.Event()
        second_shield_started = asyncio.Event()
        original_copyfileobj = shutil.copyfileobj
        original_shield = asyncio.shield

        def slow_copy(source, target, *args, **kwargs):
            copy_started.set()
            try:
                if not finish_copy.wait(timeout=2):
                    raise TimeoutError("test did not release cache copy")
                return original_copyfileobj(source, target, *args, **kwargs)
            finally:
                copy_finished.set()

        async def render() -> str:
            return str(renderer_output)

        shield_calls = 0

        def observed_shield(awaitable):
            nonlocal shield_calls
            shield_calls += 1
            if shield_calls == 2:
                second_shield_started.set()
            return original_shield(awaitable)

        async def store_with_caller_cleanup() -> Path:
            try:
                return await self.cache.store(self.date_key, self.key, render)
            finally:
                try:
                    renderer_output.unlink(missing_ok=True)
                except OSError:
                    pass

        with (
            patch(
                "astrbot_plugin_get_px.checkin.cache.shutil.copyfileobj",
                side_effect=slow_copy,
            ),
            patch(
                "astrbot_plugin_get_px.checkin.cache.asyncio.shield",
                side_effect=observed_shield,
            ),
        ):
            store_task = asyncio.create_task(store_with_caller_cleanup())
            self.assertTrue(await asyncio.to_thread(copy_started.wait, 2))
            store_task.cancel()
            await asyncio.wait_for(second_shield_started.wait(), timeout=2)
            store_task.cancel()
            finished_before_copy = store_task.done()
            source_exists_during_copy = renderer_output.exists()
            cleanup_task = asyncio.create_task(
                asyncio.to_thread(
                    self.cache.cleanup_expired,
                    today=self.next_day,
                    force=True,
                )
            )
            cleanup_result = await asyncio.gather(
                cleanup_task,
                return_exceptions=True,
            )
            finish_copy.set()
            with self.assertRaises(asyncio.CancelledError):
                await store_task
            self.assertTrue(await asyncio.to_thread(copy_finished.wait, 2))
            for _ in range(100):
                if not list((self.root / self.date_key).glob("*.tmp")):
                    break
                await asyncio.sleep(0.01)

        self.assertFalse(finished_before_copy)
        self.assertTrue(source_exists_during_copy)
        self.assertIsInstance(cleanup_result[0], int)
        final_path = self.root / self.date_key / f"{self.key}.jpg"
        self.assertTrue(final_path.exists())
        self.assertFalse(renderer_output.exists())

    async def test_cleanup_defers_active_expired_store_before_renderer_creates_directory(
        self,
    ):
        renderer_output = Path(self._tmp.name) / "renderer-output.jpg"
        _write_jpeg(renderer_output)
        renderer_started = asyncio.Event()
        finish_renderer = asyncio.Event()

        async def render() -> str:
            renderer_started.set()
            await finish_renderer.wait()
            return str(renderer_output)

        store_task = asyncio.create_task(
            self.cache.store(self.date_key, self.key, render)
        )
        await renderer_started.wait()
        self.assertFalse((self.root / self.date_key).exists())

        first_cleanup = await asyncio.to_thread(
            self.cache.cleanup_expired,
            today=self.next_day,
            force=True,
        )
        finish_renderer.set()
        stored = await store_task
        second_cleanup = self.cache.cleanup_expired(today=self.next_day)

        self.assertEqual(first_cleanup, 0)
        self.assertEqual(second_cleanup, 1)
        self.assertFalse(stored.parent.exists())

    async def test_cleanup_retries_same_day_after_active_expired_store_finishes(self):
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
            "astrbot_plugin_get_px.checkin.cache.shutil.copyfileobj",
            side_effect=slow_copy,
        ):
            store_task = asyncio.create_task(
                self.cache.store(self.date_key, self.key, render)
            )
            self.assertTrue(await asyncio.to_thread(copy_started.wait, 2))
            first_cleanup = await asyncio.to_thread(
                self.cache.cleanup_expired,
                today=self.next_day,
                force=True,
            )
            finish_copy.set()
            stored = await store_task

        second_cleanup = self.cache.cleanup_expired(today=self.next_day)

        self.assertEqual(first_cleanup, 0)
        self.assertEqual(second_cleanup, 1)
        self.assertFalse(stored.parent.exists())

    def test_cleanup_removes_previous_days_and_stale_temps_but_preserves_today(self):
        cleanup_date = date.fromisoformat(self.date_key)
        yesterday = self.root / (cleanup_date - timedelta(days=1)).isoformat()
        today = self.root / cleanup_date.isoformat()
        _write_jpeg(yesterday / "old.jpg")
        _write_jpeg(today / "current.jpg")
        stale_tmp = today / ".partial.tmp"
        stale_tmp.write_bytes(b"partial")

        removed = self.cache.cleanup_expired(today=cleanup_date, force=True)

        self.assertGreaterEqual(removed, 2)
        self.assertFalse(yesterday.exists())
        self.assertTrue((today / "current.jpg").exists())
        self.assertFalse(stale_tmp.exists())

    def test_cleanup_runs_at_most_once_for_the_same_day(self):
        cleanup_date = date.fromisoformat(self.date_key)
        self.cache.cleanup_expired(today=cleanup_date)
        late_old_dir = self.root / (cleanup_date - timedelta(days=1)).isoformat()
        _write_jpeg(late_old_dir / "old.jpg")

        self.assertEqual(self.cache.cleanup_expired(today=cleanup_date), 0)
        self.assertTrue(late_old_dir.exists())

    def test_cleanup_prunes_expired_unlocked_cache_locks(self):
        cleanup_date = date.fromisoformat(self.date_key)
        old_date = (cleanup_date - timedelta(days=1)).isoformat()
        old_lock_key = f"{old_date}:{self.key}"
        current_lock_key = f"{self.date_key}:{self.key}"
        self.cache._locks[old_lock_key] = asyncio.Lock()
        self.cache._locks[current_lock_key] = asyncio.Lock()

        self.cache.cleanup_expired(today=cleanup_date, force=True)

        self.assertNotIn(old_lock_key, self.cache._locks)
        self.assertIn(current_lock_key, self.cache._locks)

    async def test_cleanup_keeps_lock_for_store_waiting_to_enter_it(self):
        cleanup_date = date.fromisoformat(self.date_key)
        old_date = (cleanup_date - timedelta(days=1)).isoformat()
        old_lock_key = f"{old_date}:{self.key}"
        renderer_output = Path(self._tmp.name) / "waiting-render.jpg"
        _write_jpeg(renderer_output)
        lock = asyncio.Lock()
        await lock.acquire()
        self.cache._locks[old_lock_key] = lock

        task = asyncio.create_task(
            self.cache.store(old_date, self.key, lambda: renderer_output)
        )
        await asyncio.sleep(0)
        self.cache.cleanup_expired(today=cleanup_date, force=True)

        self.assertIs(self.cache._locks[old_lock_key], lock)
        lock.release()
        await task

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
