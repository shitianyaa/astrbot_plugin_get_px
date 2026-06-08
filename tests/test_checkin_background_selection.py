import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin_card import CardBackground  # noqa: E402
from astrbot_plugin_get_px.image_index import ImageIndexStore  # noqa: E402
from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402


class FakeEvent:
    def get_group_id(self):
        return "20001"

    def get_sender_id(self):
        return "10001"


class FakePixivClient:
    def __init__(self, pages, search_pages=None):
        self.pages = pages
        self.search_pages = search_pages or {}
        self.ranking_offsets = []
        self.search_calls = []

    async def ranking(self, mode: str = "week", offset: int = 0):
        self.ranking_offsets.append(offset)
        return list(self.pages.get(offset, []))

    async def search(self, tag: str, offset: int = 0):
        self.search_calls.append((tag, offset))
        return list(self.search_pages.get((tag, offset), []))


class FakeHistory:
    def __init__(self, illust_ids):
        self.illust_ids = [str(illust_id) for illust_id in illust_ids]

    async def list_records(self):
        return [{"illust_id": illust_id} for illust_id in self.illust_ids]


class FakeDownloader:
    async def download_for_send(
        self, illust, quality, proxy, timeout, downgrade_limit_bytes, log_context
    ):
        return f"picked_{illust['id']}.jpg", quality, 123


def _illust(illust_id: int) -> dict:
    return {
        "id": str(illust_id),
        "title": f"title-{illust_id}",
        "type": "illust",
        "x_restrict": 0,
        "width": 1920,
        "height": 1080,
        "user": {"name": "artist"},
        "tags": [{"name": "background"}],
    }


class CheckinBackgroundSelectionTest(unittest.IsolatedAsyncioTestCase):
    def test_split_config_tags_accepts_common_delimiters(self):
        tags = GetPxPlugin._split_config_tags("alpha，beta、gamma;delta；epsilon\nzeta")

        self.assertEqual(tags, ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"])

    def test_split_config_tags_handles_empty_and_non_string_values(self):
        for value in ["", "   ", "\n\t", None, 123]:
            with self.subTest(value=value):
                self.assertEqual(GetPxPlugin._split_config_tags(value), [])

    def test_checkin_background_tag_candidates_fallback_for_empty_values(self):
        plugin = object.__new__(GetPxPlugin)

        with patch("astrbot_plugin_get_px.main.random.shuffle") as shuffle:
            for value in ["", "   ", None]:
                with self.subTest(value=value):
                    self.assertEqual(
                        plugin._checkin_background_tag_candidates(value),
                        [""],
                    )

        shuffle.assert_not_called()

    def test_checkin_background_tag_candidates_are_randomized(self):
        plugin = object.__new__(GetPxPlugin)

        with patch(
            "astrbot_plugin_get_px.main.random.shuffle",
            side_effect=lambda tags: tags.reverse(),
        ) as shuffle:
            candidates = plugin._checkin_background_tag_candidates("alpha,beta,gamma")

        shuffle.assert_called_once()
        self.assertEqual(candidates, ["gamma", "beta", "alpha"])

    async def test_invalid_custom_background_falls_back_to_pixiv(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "checkin_background_mode": "custom",
                "checkin_custom_background": str(Path(tmp) / "missing.jpg"),
            }

            async def fake_download(event, record, *, claim_usage=True):
                self.assertTrue(claim_usage)
                return CardBackground(
                    image_path="picked.jpg",
                    mode="pixiv_daily",
                    source="rank:week",
                    illust_id="42",
                )

            plugin._download_checkin_pixiv_background = fake_download

            background = await plugin._prepare_checkin_background(
                FakeEvent(),
                SimpleNamespace(date_key="2026-05-26", user_id="10001"),
            )

            self.assertEqual(background.mode, "pixiv_daily")
            self.assertEqual(background.illust_id, "42")

    async def test_checkin_background_skips_page_used_by_index_and_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "pixiv_ranking_mode": "week",
                "pixiv_r18": 0,
                "filter_manga": True,
                "blacklist_tags": "",
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.image_history = FakeHistory(range(19))
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {
                    0: [_illust(i) for i in range(20)],
                    20: [_illust(20), _illust(21)],
                }
            )

            try:
                await plugin.image_index.record_usage(
                    scope="group:20001",
                    source_key="rank:week",
                    illust_id="19",
                    feature="checkin",
                    user_id="10001",
                )

                background = await plugin._download_checkin_pixiv_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertIsNotNone(background)
                self.assertEqual(background.illust_id, "20")
                self.assertEqual(plugin.client.ranking_offsets, [0, 20])
            finally:
                plugin.image_index.close()

    async def test_checkin_background_uses_configured_tag_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "pixiv_ranking_mode": "week",
                "pixiv_r18": 0,
                "filter_manga": True,
                "blacklist_tags": "",
                "checkin_background_tag": "blue archive，miku",
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.image_history = FakeHistory([])
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {},
                search_pages={
                    ("blue archive", 0): [_illust(50)],
                    ("miku", 0): [_illust(51)],
                },
            )

            try:
                background = await plugin._download_checkin_pixiv_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertIsNotNone(background)
                self.assertEqual(plugin.client.ranking_offsets, [])
                self.assertEqual(len(plugin.client.search_calls), 1)
                selected_tag = plugin.client.search_calls[0][0]
                self.assertIn(selected_tag, {"blue archive", "miku"})
                self.assertEqual(background.source, f"search:{selected_tag}")
            finally:
                plugin.image_index.close()

    async def test_checkin_background_returns_none_when_all_tags_have_no_result(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "pixiv_ranking_mode": "week",
                "pixiv_r18": 0,
                "filter_manga": True,
                "blacklist_tags": "",
                "checkin_background_tag": "blue archive,miku",
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.image_history = FakeHistory([])
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {},
                search_pages={
                    ("blue archive", 0): [],
                    ("miku", 0): [],
                },
            )

            try:
                with patch(
                    "astrbot_plugin_get_px.main.random.shuffle",
                    side_effect=lambda tags: None,
                ):
                    background = await plugin._download_checkin_pixiv_background(
                        FakeEvent(),
                        SimpleNamespace(date_key="2026-06-08", user_id="10001"),
                    )

                self.assertIsNone(background)
                self.assertEqual(plugin.client.ranking_offsets, [])
                self.assertEqual(
                    plugin.client.search_calls,
                    [("blue archive", 0), ("miku", 0)],
                )
            finally:
                plugin.image_index.close()

    async def test_checkin_background_tries_remaining_tags_when_first_tag_has_no_result(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "pixiv_ranking_mode": "week",
                "pixiv_r18": 0,
                "filter_manga": True,
                "blacklist_tags": "",
                "checkin_background_tag": "萝莉,ba,蔚蓝档案",
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.image_history = FakeHistory([])
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {},
                search_pages={("蔚蓝档案", 0): [_illust(60)]},
            )

            try:
                def force_random_order(tags):
                    self.assertEqual(tags, ["萝莉", "ba", "蔚蓝档案"])
                    tags[:] = ["ba", "蔚蓝档案", "萝莉"]

                with patch(
                    "astrbot_plugin_get_px.main.random.shuffle",
                    side_effect=force_random_order,
                ):
                    background = await plugin._download_checkin_pixiv_background(
                        FakeEvent(),
                        SimpleNamespace(date_key="2026-06-08", user_id="10001"),
                    )

                self.assertIsNotNone(background)
                self.assertEqual(background.source, "search:蔚蓝档案")
                self.assertEqual(
                    plugin.client.search_calls,
                    [("ba", 0), ("蔚蓝档案", 0)],
                )
            finally:
                plugin.image_index.close()

    async def test_checkin_background_claim_can_be_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "pixiv_ranking_mode": "week",
                "pixiv_r18": 0,
                "filter_manga": True,
                "blacklist_tags": "",
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.image_history = FakeHistory([])
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient({0: [_illust(30)]})

            try:
                background = await plugin._download_checkin_pixiv_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertIsNotNone(background)
                self.assertEqual(
                    await plugin.image_index.get_used_illust_ids(
                        "group:20001", "rank:week"
                    ),
                    {"30"},
                )

                await plugin._release_checkin_background_claim(FakeEvent(), background)

                self.assertEqual(
                    await plugin.image_index.get_used_illust_ids(
                        "group:20001", "rank:week"
                    ),
                    set(),
                )
            finally:
                plugin.image_index.close()


if __name__ == "__main__":
    unittest.main()
