import asyncio
import tempfile
import unittest
from pathlib import Path

from image_index import ImageIndexStore


class FrozenImageIndexStore(ImageIndexStore):
    def __init__(self, data_dir: Path | str, *, date_key: str = "2026-05-26"):
        self.date_key = date_key
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"


class ImageIndexStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_claim_usage_is_atomic_and_release_allows_reclaim(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)

            results = await asyncio.gather(
                *[
                    store.claim_usage(
                        scope="group:1",
                        source_key="rank:week",
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
                await store.get_used_illust_ids("group:1", "rank:week"), {"100"}
            )

            await store.release_usage(
                scope="group:1",
                source_key="rank:week",
                illust_id="100",
                feature="normal_pending",
            )

            self.assertEqual(
                await store.get_used_illust_ids("group:1", "rank:week"), set()
            )
            self.assertTrue(
                await store.claim_usage(
                    scope="group:1",
                    source_key="rank:week",
                    illust_id="100",
                    feature="normal_pending",
                    user_id="user:next",
                )
            )

    async def test_release_does_not_delete_successful_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)

            await store.record_usage(
                scope="group:1",
                source_key="rank:week",
                illust_id="101",
                feature="normal",
                user_id="user:1",
            )
            await store.release_usage(
                scope="group:1",
                source_key="rank:week",
                illust_id="101",
                feature="normal_pending",
            )

            self.assertEqual(
                await store.get_used_illust_ids("group:1", "rank:week"), {"101"}
            )

    async def test_cleanup_old_days_clears_usage_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")

            await store.record_usage(
                scope="group:1",
                source_key="rank:week",
                illust_id="200",
                feature="normal",
                user_id="user:1",
            )
            store.date_key = "2026-05-27"
            await store.cleanup_old_days()

            self.assertEqual(
                await store.get_used_illust_ids("group:1", "rank:week"), set()
            )

    async def test_blacklist_persists_across_day_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")

            added = await store.add_blacklist_illust(
                illust_id="300",
                title="blocked",
                author="artist",
                source="rank:week",
                record_id="record-1",
            )
            store.date_key = "2026-05-27"
            await store.cleanup_old_days()

            self.assertTrue(added)
            self.assertTrue(await store.is_blacklisted("300"))
            self.assertEqual(await store.get_blacklisted_illust_ids(), {"300"})

    async def test_blacklist_list_order_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")

            await store.add_blacklist_illust(
                illust_id="301",
                title="older",
                author="artist-a",
                source="rank:week",
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

    async def test_blacklist_thumbnail_is_copied_and_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_path = Path(tmp) / "source.jpg"
            source_path.write_bytes(b"fake-jpeg-thumbnail")
            store = FrozenImageIndexStore(tmp)

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


if __name__ == "__main__":
    unittest.main()
