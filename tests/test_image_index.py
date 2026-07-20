import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pixiv.index as image_index_module
from pixiv.index import ImageIndexStore


class FrozenImageIndexStore(ImageIndexStore):
    def __init__(
        self,
        data_dir: Path | str,
        *,
        date_key: str = "2026-05-26",
        retention_days: int = 1,
    ):
        self.date_key = date_key
        self._frozen_dt = datetime.fromisoformat(f"{date_key}T12:00:00+08:00")
        super().__init__(data_dir, retention_days=retention_days)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"

    def _now_dt(self) -> datetime:
        return self._frozen_dt


class ImageIndexStoreTest(unittest.IsolatedAsyncioTestCase):
    def test_today_key_uses_beijing_calendar_day_at_utc_boundary(self):
        utc_before_midnight = datetime(2026, 7, 20, 15, 59, 59, tzinfo=timezone.utc)
        utc_after_midnight = datetime(2026, 7, 20, 16, 0, 0, tzinfo=timezone.utc)

        with mock.patch.object(image_index_module, "datetime") as fake_datetime:
            fake_datetime.now.side_effect = lambda tz: utc_before_midnight.astimezone(tz)
            self.assertEqual(ImageIndexStore.today_key(), "2026-07-20")
            fake_datetime.now.side_effect = lambda tz: utc_after_midnight.astimezone(tz)
            self.assertEqual(ImageIndexStore.today_key(), "2026-07-21")

    async def test_claim_usage_is_atomic_and_release_allows_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                results = await asyncio.gather(
                    *[
                        store.claim_usage(
                            scope="group:1",
                            source_key="pixiv:recommended",
                            illust_id="100",
                            feature="normal_pending",
                            user_id=f"user:{idx}",
                        )
                        for idx in range(20)
                    ]
                )

                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), 19)
                self.assertEqual(
                    await store.get_used_illust_ids("group:1", "pixiv:recommended"), {"100"}
                )

                await store.release_usage(
                    scope="group:1",
                    source_key="pixiv:recommended",
                    illust_id="100",
                    feature="normal_pending",
                )

                self.assertEqual(
                    await store.get_used_illust_ids("group:1", "pixiv:recommended"), set()
                )
                self.assertTrue(
                    await store.claim_usage(
                        scope="group:1",
                        source_key="pixiv:recommended",
                        illust_id="100",
                        feature="normal_pending",
                        user_id="user:next",
                    )
                )
            finally:
                store.close()

    async def test_release_does_not_delete_successful_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                await store.record_usage(
                    scope="group:1",
                        source_key="pixiv:recommended",
                    illust_id="101",
                    feature="normal",
                    user_id="user:1",
                )
                await store.release_usage(
                    scope="group:1",
                        source_key="pixiv:recommended",
                    illust_id="101",
                    feature="normal_pending",
                )

                self.assertEqual(
                    await store.get_used_illust_ids("group:1", "pixiv:recommended"), {"101"}
                )
            finally:
                store.close()

    async def test_cleanup_old_days_clears_usage_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")
            try:
                await store.record_usage(
                    scope="group:1",
                        source_key="pixiv:recommended",
                    illust_id="200",
                    feature="normal",
                    user_id="user:1",
                )
                store.date_key = "2026-05-27"
                await store.cleanup_old_days()

                self.assertEqual(
                    await store.get_used_illust_ids("group:1", "pixiv:recommended"), set()
                )
            finally:
                store.close()

    async def test_startup_cleanup_logs_result_even_when_nothing_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=1)
            try:
                with mock.patch.object(image_index_module, "logger") as mocked_logger:
                    removed = await store.cleanup_old_days(trigger="startup")

                self.assertEqual(removed, 0)
                message = str(mocked_logger.info.call_args.args[0])
                self.assertIn("触发方式=startup", message)
                self.assertIn("保留天数=1", message)
                self.assertIn("清理数量=0", message)
                self.assertIn("状态=启用", message)
            finally:
                store.close()

    async def test_startup_cleanup_throttles_immediate_write_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=1)
            try:
                with (
                    mock.patch.object(
                        store,
                        "_cleanup_old_days_sync",
                        wraps=store._cleanup_old_days_sync,
                    ) as cleanup,
                    mock.patch.object(
                        image_index_module.time, "monotonic", return_value=7200
                    ),
                ):
                    await store.cleanup_old_days(trigger="startup")
                    await store.record_usage(
                        scope="group:1",
                        source_key="pixiv:recommended",
                        illust_id="startup-cleanup",
                        feature="normal",
                    )

                self.assertEqual(cleanup.call_count, 1)
                self.assertEqual(store._last_cleanup_ts, 7200)
            finally:
                store.close()

    async def test_failed_retention_cleanup_restores_previous_window_and_redacts_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=7)
            try:
                with (
                    mock.patch.object(
                        store,
                        "_cleanup_old_days_sync",
                        side_effect=RuntimeError("https://private.example/secret"),
                    ),
                    mock.patch.object(image_index_module, "logger") as mocked_logger,
                ):
                    with self.assertRaises(RuntimeError):
                        await store.set_retention_days(1)

                self.assertEqual(store.retention_days, 7)
                message = str(mocked_logger.warning.call_args.args[0])
                self.assertIn("原天数=7", message)
                self.assertIn("新天数=1", message)
                self.assertIn("错误类型=RuntimeError", message)
                self.assertNotIn("private.example", message)
                self.assertNotIn("secret", message)
            finally:
                store.close()

    def test_failed_periodic_cleanup_is_not_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=1)
            try:
                with (
                    mock.patch.object(
                        store,
                        "_cleanup_old_days_sync",
                        side_effect=[RuntimeError("locked"), 0],
                    ) as cleanup,
                    mock.patch.object(image_index_module.time, "monotonic", return_value=7200),
                ):
                    with self.assertRaises(RuntimeError):
                        store._maybe_cleanup_old_days_sync(store.today_key())
                    self.assertEqual(store._last_cleanup_ts, 0.0)

                    store._maybe_cleanup_old_days_sync(store.today_key())

                self.assertEqual(cleanup.call_count, 2)
                self.assertEqual(store._last_cleanup_ts, 7200)
            finally:
                store.close()

    async def test_claim_and_release_failures_log_safe_operation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=7)
            try:
                with (
                    mock.patch.object(image_index_module, "logger") as mocked_logger,
                    mock.patch.object(
                        store,
                        "_claim_usage_sync",
                        side_effect=sqlite3.OperationalError(
                            "https://private.example/claim?token=secret"
                        ),
                    ),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        await store.claim_usage(
                            scope="private:user-secret",
                            source_key="search:private-tag",
                            illust_id="private-illust",
                            feature="normal_pending",
                            user_id="user-secret",
                        )

                claim_message = str(mocked_logger.warning.call_args.args[0])
                self.assertIn("操作=占用", claim_message)
                self.assertIn("功能=normal_pending", claim_message)
                self.assertIn("错误类型=OperationalError", claim_message)
                self.assertNotIn("private.example", claim_message)
                self.assertNotIn("user-secret", claim_message)
                self.assertNotIn("private-tag", claim_message)
                self.assertNotIn("private-illust", claim_message)

                with (
                    mock.patch.object(image_index_module, "logger") as mocked_logger,
                    mock.patch.object(
                        store,
                        "_release_usage_sync",
                        side_effect=sqlite3.OperationalError(
                            "https://private.example/release?token=secret"
                        ),
                    ),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        await store.release_usage(
                            scope="private:user-secret",
                            source_key="search:private-tag",
                            illust_id="private-illust",
                            feature="normal_pending",
                        )

                release_message = str(mocked_logger.warning.call_args.args[0])
                self.assertIn("操作=释放", release_message)
                self.assertIn("功能=normal_pending", release_message)
                self.assertIn("错误类型=OperationalError", release_message)
                self.assertNotIn("private.example", release_message)
                self.assertNotIn("user-secret", release_message)
                self.assertNotIn("private-tag", release_message)
                self.assertNotIn("private-illust", release_message)
            finally:
                store.close()

    async def test_record_failure_logs_safe_operation_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=7)
            try:
                with (
                    mock.patch.object(image_index_module, "logger") as mocked_logger,
                    mock.patch.object(
                        store,
                        "_record_usage_sync",
                        side_effect=sqlite3.OperationalError(
                            "https://private.example/record?token=secret"
                        ),
                    ),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        await store.record_usage(
                            scope="private:user-secret",
                            source_key="search:private-tag",
                            illust_id="private-illust",
                            feature="normal",
                            user_id="user-secret",
                        )

                message = str(mocked_logger.warning.call_args.args[0])
                self.assertIn("操作=记录占用", message)
                self.assertIn("功能=normal", message)
                self.assertIn("错误类型=OperationalError", message)
                self.assertNotIn("private.example", message)
                self.assertNotIn("user-secret", message)
                self.assertNotIn("private-tag", message)
                self.assertNotIn("private-illust", message)
            finally:
                store.close()

    async def test_release_removes_pending_claim_from_previous_beijing_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2026-07-20", retention_days=7
            )
            try:
                self.assertTrue(
                    await store.claim_usage(
                        scope="group:1",
                        source_key="lolicon:tag",
                        illust_id="midnight-pending",
                        feature="normal_pending",
                    )
                )
                store.date_key = "2026-07-21"
                store._frozen_dt = datetime.fromisoformat(
                    "2026-07-21T00:00:01+08:00"
                )

                await store.release_usage(
                    scope="group:1",
                    source_key="lolicon:tag",
                    illust_id="midnight-pending",
                    feature="normal_pending",
                )

                self.assertNotIn(
                    "midnight-pending",
                    await store.get_used_illust_ids("group:1", "lolicon:tag"),
                )
            finally:
                store.close()

    async def test_release_only_removes_the_latest_matching_pending_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2026-07-21", retention_days=7
            )
            try:
                conn = store._get_conn()
                for date_key in ("2026-07-19", "2026-07-20"):
                    conn.execute(
                        """
                        INSERT INTO image_usage (
                            date_key, scope, source_key, illust_id,
                            feature, user_id, sent_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            date_key,
                            "group:1",
                            "lolicon:tag",
                            "legacy-pending",
                            "normal_pending",
                            "",
                            f"{date_key}T12:00:00+08:00",
                        ),
                    )
                conn.commit()

                await store.release_usage(
                    scope="group:1",
                    source_key="lolicon:tag",
                    illust_id="legacy-pending",
                    feature="normal_pending",
                )

                remaining = conn.execute(
                    """
                    SELECT date_key FROM image_usage
                    WHERE scope = ? AND source_key = ? AND illust_id = ?
                    ORDER BY date_key
                    """,
                    ("group:1", "lolicon:tag", "legacy-pending"),
                ).fetchall()
                self.assertEqual([row["date_key"] for row in remaining], ["2026-07-19"])
            finally:
                store.close()

    async def test_claim_usage_is_atomic_across_store_connections(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = FrozenImageIndexStore(tmp, retention_days=7)
            second = FrozenImageIndexStore(tmp, retention_days=7)
            try:
                stores = [first, second] * 10
                results = await asyncio.gather(
                    *[
                        store.claim_usage(
                            scope="group:1",
                            source_key="pixiv:recommended",
                            illust_id="100-shared",
                            feature="normal_pending",
                            user_id=f"user:{idx}",
                        )
                        for idx, store in enumerate(stores)
                    ]
                )

                self.assertEqual(results.count(True), 1)
                self.assertEqual(results.count(False), 19)
            finally:
                first.close()
                second.close()

    async def test_seven_day_window_includes_six_previous_natural_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2026-05-20", retention_days=7
            )
            try:
                await store.record_usage(
                    scope="group:1",
                    source_key="pixiv:recommended",
                    illust_id="201",
                    feature="normal",
                )

                store.date_key = "2026-05-26"
                self.assertEqual(
                    await store.get_used_illust_ids(
                        "group:1", "pixiv:recommended"
                    ),
                    {"201"},
                )

                store.date_key = "2026-05-27"
                await store.cleanup_old_days()
                self.assertEqual(
                    await store.get_used_illust_ids(
                        "group:1", "pixiv:recommended"
                    ),
                    set(),
                )
            finally:
                store.close()

    async def test_retention_window_crosses_year_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2025-12-31", retention_days=2
            )
            try:
                await store.record_usage(
                    scope="private:1",
                    source_key="search:new-year",
                    illust_id="201-year",
                    feature="normal",
                )
                store.date_key = "2026-01-01"

                self.assertEqual(
                    await store.get_used_illust_ids(
                        "private:1", "search:new-year"
                    ),
                    {"201-year"},
                )
            finally:
                store.close()

    async def test_shortening_retention_immediately_removes_expired_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2026-05-20", retention_days=7
            )
            try:
                await store.record_usage(
                    scope="group:1",
                    source_key="pixiv:recommended",
                    illust_id="202",
                    feature="normal",
                )
                store.date_key = "2026-05-21"

                with mock.patch.object(image_index_module, "logger") as mocked_logger:
                    days = await store.set_retention_days(1)

                self.assertEqual(days, 1)
                self.assertEqual(store.retention_days, 1)
                self.assertEqual(
                    await store.get_used_illust_ids(
                        "group:1", "pixiv:recommended"
                    ),
                    set(),
                )
                message = str(mocked_logger.info.call_args.args[0])
                self.assertIn("原天数=7", message)
                self.assertIn("当前天数=1", message)
                self.assertIn("清理数量=1", message)
                self.assertIn("状态=启用", message)
            finally:
                store.close()

    async def test_retention_cleanup_throttles_immediate_write_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=7)
            try:
                with (
                    mock.patch.object(
                        store,
                        "_cleanup_old_days_sync",
                        wraps=store._cleanup_old_days_sync,
                    ) as cleanup,
                    mock.patch.object(
                        image_index_module.time, "monotonic", return_value=7200
                    ),
                ):
                    await store.set_retention_days(1)
                    await store.record_usage(
                        scope="group:1",
                        source_key="pixiv:recommended",
                        illust_id="retention-cleanup",
                        feature="normal",
                    )

                self.assertEqual(cleanup.call_count, 1)
                self.assertEqual(store._last_cleanup_ts, 7200)
            finally:
                store.close()

    async def test_zero_days_clears_usage_and_disables_future_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, retention_days=1)
            try:
                await store.record_usage(
                    scope="group:1",
                    source_key="pixiv:recommended",
                    illust_id="203",
                    feature="normal",
                )

                with mock.patch.object(image_index_module, "logger") as mocked_logger:
                    await store.set_retention_days(0)

                self.assertEqual(
                    await store.get_used_illust_ids(
                        "group:1", "pixiv:recommended"
                    ),
                    set(),
                )
                self.assertTrue(
                    await store.claim_usage(
                        scope="group:1",
                        source_key="pixiv:recommended",
                        illust_id="203",
                        feature="normal_pending",
                    )
                )
                self.assertTrue(
                    await store.claim_usage(
                        scope="group:1",
                        source_key="pixiv:recommended",
                        illust_id="203",
                        feature="normal_pending",
                    )
                )
                await store.record_usage(
                    scope="group:1",
                    source_key="pixiv:recommended",
                    illust_id="204",
                    feature="normal",
                )
                row = store._get_conn().execute(
                    "SELECT COUNT(*) AS count FROM image_usage"
                ).fetchone()
                self.assertEqual(row["count"], 0)
                message = str(mocked_logger.info.call_args.args[0])
                self.assertIn("当前天数=0", message)
                self.assertIn("清理数量=1", message)
                self.assertIn("状态=关闭", message)
            finally:
                store.close()

    async def test_claim_rejects_usage_from_previous_day_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2026-05-25", retention_days=7
            )
            try:
                await store.record_usage(
                    scope="group:1",
                    source_key="pixiv:recommended",
                    illust_id="205",
                    feature="normal",
                )
                store.date_key = "2026-05-26"

                results = await asyncio.gather(
                    *[
                        store.claim_usage(
                            scope="group:1",
                            source_key="pixiv:recommended",
                            illust_id="205",
                            feature="normal_pending",
                            user_id=f"user:{idx}",
                        )
                        for idx in range(10)
                    ]
                )

                self.assertEqual(results, [False] * 10)
            finally:
                store.close()

    async def test_usage_window_keeps_scope_and_source_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(
                tmp, date_key="2026-05-25", retention_days=7
            )
            try:
                await store.record_usage(
                    scope="group:1",
                    source_key="search:tag-a",
                    illust_id="206",
                    feature="normal",
                )
                store.date_key = "2026-05-26"

                self.assertFalse(
                    await store.claim_usage(
                        scope="group:1",
                        source_key="search:tag-a",
                        illust_id="206",
                        feature="normal_pending",
                    )
                )
                self.assertTrue(
                    await store.claim_usage(
                        scope="group:2",
                        source_key="search:tag-a",
                        illust_id="206",
                        feature="normal_pending",
                    )
                )
                self.assertTrue(
                    await store.claim_usage(
                        scope="group:1",
                        source_key="search:tag-b",
                        illust_id="206",
                        feature="normal_pending",
                    )
                )
            finally:
                store.close()

    async def test_usage_cleanup_preserves_safety_terms_and_page_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-25")
            try:
                await store.record_usage(
                    scope="group:1",
                    source_key="search:tag",
                    illust_id="207",
                    feature="normal",
                )
                await store.add_safety_term("危险主题", added_by="test")
                await store.advance_page_offset("group:1", "search:tag", 30)
                store.date_key = "2026-05-26"
                store._frozen_dt = datetime.fromisoformat(
                    "2026-05-26T12:00:00+08:00"
                )

                await store.cleanup_old_days()

                self.assertIn(
                    "危险主题".casefold(), await store.get_custom_safety_terms()
                )
                self.assertEqual(
                    await store.get_page_offset("group:1", "search:tag"), 30
                )
            finally:
                store.close()

    async def test_blacklist_persists_across_day_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")
            try:
                added = await store.add_blacklist_illust(
                    illust_id="300",
                    title="blocked",
                    author="artist",
                    source="pixiv:recommended",
                    record_id="record-1",
                )
                store.date_key = "2026-05-27"
                await store.cleanup_old_days()

                self.assertTrue(added)
                self.assertTrue(await store.is_blacklisted("300"))
                self.assertEqual(await store.get_blacklisted_illust_ids(), {"300"})
            finally:
                store.close()

    async def test_blacklist_list_order_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")
            try:
                await store.add_blacklist_illust(
                    illust_id="301",
                    title="older",
                    author="artist-a",
                    source="pixiv:recommended",
                    record_id="record-301",
                )
                store.date_key = "2026-05-27"
                await store.add_blacklist_illust(
                    illust_id="302",
                    title="newer",
                    author="artist-b",
                    source="search:test",
                    record_id="record-302",
                )

                records = await store.list_blacklist_illusts()

                self.assertEqual(
                    [record["illust_id"] for record in records], ["302", "301"]
                )
                self.assertEqual(records[0]["title"], "newer")

                removed = await store.remove_blacklist_illust("301")

                self.assertIsNotNone(removed)
                self.assertEqual(removed["illust_id"], "301")
                self.assertFalse(await store.is_blacklisted("301"))
                self.assertEqual(await store.get_blacklisted_illust_ids(), {"302"})
            finally:
                store.close()

    async def test_custom_safety_terms_and_blacklist_reason_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                self.assertTrue(
                    await store.add_safety_term("危险主题", added_by="admin")
                )
                self.assertIn(
                    "危险主题".casefold(), await store.get_custom_safety_terms()
                )
                await store.add_blacklist_illust(
                    illust_id="350",
                    reason="构图不适合",
                    added_by="web",
                )
                record = (await store.list_blacklist_illusts())[0]
                self.assertEqual(record["reason"], "构图不适合")
                self.assertEqual(record["added_by"], "web")
                self.assertTrue(await store.remove_safety_term("危险主题"))
                self.assertEqual(await store.get_custom_safety_terms(), set())
            finally:
                store.close()

    async def test_blacklist_thumbnail_is_copied_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.jpg"
            source_path.write_bytes(b"fake-jpeg-thumbnail")
            store = FrozenImageIndexStore(tmp)
            try:
                thumb_id = await store.save_blacklist_thumbnail(
                    illust_id="400",
                    source_path=source_path,
                )
                await store.add_blacklist_illust(
                    illust_id="400",
                    title="blocked with thumb",
                    thumb_id=thumb_id,
                )

                thumb_path = await store.get_blacklist_thumbnail_path("400")

                self.assertEqual(thumb_id, "400.jpg")
                self.assertIsNotNone(thumb_path)
                self.assertEqual(thumb_path.read_bytes(), b"fake-jpeg-thumbnail")

                removed = await store.remove_blacklist_illust("400")

                self.assertIsNotNone(removed)
                self.assertEqual(removed["thumb_id"], "400.jpg")
                self.assertIsNone(await store.get_blacklist_thumbnail_path("400"))
            finally:
                store.close()

    # ── page-cursor tests ──────────────────────────────────────────

    async def test_page_offset_starts_at_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                offset = await store.get_page_offset("group:1", "search:blue_archive")
                self.assertEqual(offset, 0)
            finally:
                store.close()

    async def test_advance_page_offset_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                new_offset = await store.advance_page_offset(
                    "group:1", "search:blue_archive", results_count=30
                )
                self.assertEqual(new_offset, 30)
                # 再次查询应返回新游标
                offset = await store.get_page_offset("group:1", "search:blue_archive")
                self.assertEqual(offset, 30)
            finally:
                store.close()

    async def test_advance_page_offset_second_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                # 第一页
                await store.advance_page_offset("group:1", "search:blue_archive", 30)
                # 第二页
                new_offset = await store.advance_page_offset(
                    "group:1", "search:blue_archive", results_count=30
                )
                self.assertEqual(new_offset, 60)
            finally:
                store.close()

    async def test_advance_page_offset_resets_when_below_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                await store.advance_page_offset("group:1", "search:rare_tag", 30)
                # 返回结果 < 20 时应重置为 0
                new_offset = await store.advance_page_offset(
                    "group:1", "search:rare_tag", results_count=15
                )
                self.assertEqual(new_offset, 0)
            finally:
                store.close()

    async def test_advance_page_offset_with_raw_count_matters(self):
        """过滤后少但原始 ≥ 20 不应重置（修 blocking bug）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                await store.advance_page_offset("group:1", "search:tag", 30)
                # 原始 30 条（≥ 阈值 20），不应重置
                new_offset = await store.advance_page_offset(
                    "group:1", "search:tag", results_count=30
                )
                self.assertEqual(new_offset, 60)
            finally:
                store.close()

    async def test_page_offset_ttl_expired(self):
        """过期游标应返回 0 并删除记录。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                # 先写入一个正常游标
                new_offset = await store.advance_page_offset(
                    "group:1", "search:blue_archive", results_count=30
                )
                self.assertEqual(new_offset, 30)

                # 直接修改数据库，把 updated_at 改成 PAGE_CURSOR_TTL_DAYS+1 天前
                from pixiv.index import PAGE_CURSOR_TTL_DAYS
                from datetime import timedelta

                old_time = store._frozen_dt - timedelta(days=PAGE_CURSOR_TTL_DAYS + 1)
                # 关闭 store 的连接，用独立连接修改数据库
                store.close()
                from contextlib import closing

                with closing(sqlite3.connect(str(store._db_path))) as conn:
                    conn.execute(
                        "UPDATE source_page_cursor SET updated_at = ? "
                        "WHERE scope = ? AND source_key = ?",
                        (
                            old_time.isoformat(timespec="seconds"),
                            "group:1",
                            "search:blue_archive",
                        ),
                    )
                    conn.commit()
                # 重新获取连接
                store._conn = None

                # 现在查询应该返回 0（TTL 过期）
                offset = await store.get_page_offset("group:1", "search:blue_archive")
                self.assertEqual(offset, 0)
            finally:
                store.close()

    async def test_page_offset_ttl_valid(self):
        """未过期游标应正常返回。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                await store.advance_page_offset(
                    "group:1", "search:blue_archive", results_count=30
                )
                # 设置 updated_at 为 TTL 内（已包含在 advance 中，直接验证）
                offset = await store.get_page_offset("group:1", "search:blue_archive")
                self.assertEqual(offset, 30)
            finally:
                store.close()

    async def test_page_offset_clears_after_reset(self):
        """重置后再次查询应为 0。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)
            try:
                await store.advance_page_offset("group:1", "search:tag", 30)
                # 到底重置
                new_offset = await store.advance_page_offset(
                    "group:1", "search:tag", results_count=10
                )
                self.assertEqual(new_offset, 0)
                # 再次查询确认
                offset = await store.get_page_offset("group:1", "search:tag")
                self.assertEqual(offset, 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
