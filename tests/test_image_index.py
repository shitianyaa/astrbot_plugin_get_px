import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from pixiv.index import ImageIndexStore


class FrozenImageIndexStore(ImageIndexStore):
    def __init__(self, data_dir: Path | str, *, date_key: str = "2026-05-26"):
        self.date_key = date_key
        self._frozen_dt = datetime.fromisoformat(f"{date_key}T12:00:00+08:00")
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"

    def _now_dt(self) -> datetime:
        return self._frozen_dt


class ImageIndexStoreTest(unittest.IsolatedAsyncioTestCase):
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
