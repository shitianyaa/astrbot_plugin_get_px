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
                        feature="fortune_pending",
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
                feature="fortune_pending",
            )

            self.assertEqual(
                await store.get_used_illust_ids("group:1", "rank:week"), set()
            )
            self.assertTrue(
                await store.claim_usage(
                    scope="group:1",
                    source_key="rank:week",
                    illust_id="100",
                    feature="fortune_pending",
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
                feature="fortune",
                user_id="user:1",
            )
            await store.release_usage(
                scope="group:1",
                source_key="rank:week",
                illust_id="101",
                feature="fortune_pending",
            )

            self.assertEqual(
                await store.get_used_illust_ids("group:1", "rank:week"), {"101"}
            )

    async def test_fortune_record_keeps_same_day_text_image_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp)

            await store.save_fortune_record(
                scope="group:1",
                user_id="user:1",
                source_key="fortune",
                fortune_text="first",
            )
            await store.save_fortune_record(
                scope="group:1",
                user_id="user:1",
                source_key="fortune",
                fortune_text="second",
                illust_id="999",
                image_source_key="rank:day",
            )
            record = await store.claim_fortune_illust_id(
                scope="group:1",
                user_id="user:1",
                source_key="fortune",
                illust_id="123",
                image_source_key="rank:week",
            )

            self.assertIsNotNone(record)
            self.assertEqual(record.fortune_text, "first")
            self.assertEqual(record.illust_id, "123")
            self.assertEqual(record.image_source_key, "rank:week")

            second_claim = await store.claim_fortune_illust_id(
                scope="group:1",
                user_id="user:1",
                source_key="fortune",
                illust_id="456",
                image_source_key="rank:month",
            )

            self.assertIsNotNone(second_claim)
            self.assertEqual(second_claim.fortune_text, "first")
            self.assertEqual(second_claim.illust_id, "123")
            self.assertEqual(second_claim.image_source_key, "rank:week")

    async def test_cleanup_old_days_clears_usage_and_fortune_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenImageIndexStore(tmp, date_key="2026-05-26")

            await store.record_usage(
                scope="group:1",
                source_key="rank:week",
                illust_id="200",
                feature="fortune",
                user_id="user:1",
            )
            await store.save_fortune_record(
                scope="group:1",
                user_id="user:1",
                source_key="fortune",
                fortune_text="today",
            )
            store.date_key = "2026-05-27"
            await store.cleanup_old_days()

            self.assertEqual(
                await store.get_used_illust_ids("group:1", "rank:week"), set()
            )
            self.assertIsNone(
                await store.get_fortune_record(
                    scope="group:1",
                    user_id="user:1",
                    source_key="fortune",
                )
            )


if __name__ == "__main__":
    unittest.main()
