import sys
import unittest
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore


class FilteringTest(unittest.TestCase):
    def test_filter_manga_removes_manga_from_mixed_results(self):
        illusts = [
            {"id": "1", "type": "manga"},
            {"id": "2", "type": "illust"},
            {"id": "3", "type": "ugoira"},
        ]

        filtered = FiltersMixin._filter_manga(illusts)

        self.assertEqual([illust["id"] for illust in filtered], ["2", "3"])

    def test_safe_rating_only_keeps_general_audience(self):
        filtered = FiltersMixin._filter_safe_rating(
            [
                {"id": "1", "x_restrict": 0},
                {"id": "2", "x_restrict": 1},
                {"id": "3", "x_restrict": 2},
            ]
        )
        self.assertEqual([item["id"] for item in filtered], ["1"])


class SafetyFilteringTest(unittest.IsolatedAsyncioTestCase):
    async def test_builtin_and_custom_terms_filter_queries_and_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixin = object.__new__(FiltersMixin)
            mixin.image_index = ImageIndexStore(tmp)
            try:
                self.assertTrue(await mixin._blocked_query_term("g u r o illustration"))
                await mixin.image_index.add_safety_term("危险主题", added_by="test")
                self.assertTrue(await mixin._blocked_query_term("危险-主题 壁纸"))
                filtered = await mixin._filter_blacklisted_illusts(
                    [
                        {"id": "1", "x_restrict": 0, "title": "safe", "tags": []},
                        {"id": "2", "x_restrict": 0, "title": "危险主题", "tags": []},
                        {"id": "3", "x_restrict": 1, "title": "safe", "tags": []},
                    ]
                )
                self.assertEqual([item["id"] for item in filtered], ["1"])
            finally:
                mixin.image_index.close()

    async def test_blacklist_store_failure_is_fail_closed(self):
        class BrokenIndex:
            async def get_custom_safety_terms(self):
                return set()

            async def get_blacklisted_illust_ids(self):
                raise OSError("database unavailable")

        mixin = object.__new__(FiltersMixin)
        mixin.image_index = BrokenIndex()
        with self.assertRaisesRegex(RuntimeError, "内容安全服务暂不可用"):
            await mixin._filter_blacklisted_illusts(
                [{"id": "1", "x_restrict": 0, "title": "safe", "tags": []}]
            )


if __name__ == "__main__":
    unittest.main()
