import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin import background as checkin_background  # noqa: E402
from astrbot_plugin_get_px.checkin.card import CardBackground  # noqa: E402
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore  # noqa: E402
from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402


class FakeEvent:
    def get_group_id(self):
        return "20001"

    def get_sender_id(self):
        return "10001"


class FakePixivClient:
    def __init__(self, pages):
        self.pages = pages
        self.ranking_offsets = []

    async def ranking(self, mode: str = "week", offset: int = 0):
        self.ranking_offsets.append(offset)
        return list(self.pages.get(offset, []))

    async def search(self, tag: str, offset: int = 0):
        return []


class FakeHistory:
    def __init__(self, illust_ids):
        self.illust_ids = [str(illust_id) for illust_id in illust_ids]

    async def list_records(self):
        return [{"illust_id": illust_id} for illust_id in self.illust_ids]


class FakeDownloader:
    def __init__(self):
        self.illust_ids = []

    async def download_for_send(
        self, illust, quality, proxy, timeout, downgrade_limit_bytes, log_context
    ):
        self.illust_ids.append(str(illust["id"]))
        return f"picked_{illust['id']}.jpg", quality, 123


def _illust(illust_id: int, *, width: int = 750, height: int = 1000) -> dict:
    return {
        "id": str(illust_id),
        "title": f"title-{illust_id}",
        "type": "illust",
        "x_restrict": 0,
        "width": width,
        "height": height,
        "user": {"name": "artist"},
        "tags": [{"name": "background"}],
    }


class CheckinBackgroundSelectionTest(unittest.IsolatedAsyncioTestCase):
    def test_custom_background_schema_recommends_portrait_contain_display(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        hint = schema["checkin_custom_background"]["hint"]

        for landscape_contract in ("16:9", "1920x1080", "960x540"):
            with self.subTest(landscape_contract=landscape_contract):
                self.assertNotIn(landscape_contract, hint)
        for portrait_contract in ("3:4", "竖", "contain", "不裁切"):
            with self.subTest(portrait_contract=portrait_contract):
                self.assertIn(portrait_contract, hint)

    def test_checkin_artwork_ratio_uses_fixed_closed_portrait_range(self):
        self.assertEqual(
            getattr(checkin_background, "CHECKIN_ARTWORK_TARGET_RATIO", None), 0.75
        )
        self.assertEqual(
            getattr(checkin_background, "CHECKIN_ARTWORK_TOLERANCE", None), 0.20
        )

        for ratio in (0.60, 0.75, 0.90):
            with self.subTest(ratio=ratio):
                self.assertTrue(
                    checkin_background.aspect_ratio_matches(
                        _illust(1, width=round(ratio * 100), height=100),
                        0.75,
                        0.20,
                    )
                )

        for ratio in (0.59, 1.0, 1.77):
            with self.subTest(ratio=ratio):
                self.assertFalse(
                    checkin_background.aspect_ratio_matches(
                        _illust(1, width=round(ratio * 100), height=100),
                        0.75,
                        0.20,
                    )
                )

    async def test_invalid_custom_background_falls_back_to_pixiv(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "checkin_background_mode": "custom",
                "checkin_custom_background": str(Path(tmp) / "missing.jpg"),
            }

            async def fake_download(
                event,
                record,
                *,
                claim_usage=True,
                preview_nonce=0,
                preview_excluded_ids=None,
            ):
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

    async def test_preview_background_refreshes_for_five_minutes_without_index_writes(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "checkin_background_mode": "pixiv_daily",
                "pixiv_refresh_token": "token",
                "pixiv_ranking_mode": "week",
                "pixiv_r18": 0,
                "filter_manga": True,
                "blacklist_tags": "",
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.image_history = FakeHistory([])
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient({0: [_illust(70), _illust(71)]})
            record = SimpleNamespace(date_key="2026-07-12", user_id="10001")

            try:
                first = await plugin._prepare_checkin_background(
                    FakeEvent(),
                    record,
                    claim_usage=False,
                    refresh_preview=True,
                )
                second = await plugin._prepare_checkin_background(
                    FakeEvent(),
                    record,
                    claim_usage=False,
                    refresh_preview=True,
                )
                plugin._checkin_preview_background_ids["10001"] = [
                    (illust_id, created_at - 301.0)
                    for illust_id, created_at in plugin._checkin_preview_background_ids[
                        "10001"
                    ]
                ]
                third = await plugin._prepare_checkin_background(
                    FakeEvent(),
                    record,
                    claim_usage=False,
                    refresh_preview=True,
                )

                self.assertNotEqual(first.illust_id, second.illust_id)
                self.assertIn(third.illust_id, {"70", "71"})
                self.assertEqual(
                    len(plugin._checkin_preview_background_ids["10001"]), 1
                )
                self.assertEqual(
                    await plugin.image_index.get_used_illust_ids(
                        "group:20001", "rank:week"
                    ),
                    set(),
                )
                self.assertEqual(plugin.client.ranking_offsets, [0, 0, 0])
            finally:
                plugin.image_index.close()

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

    async def test_landscape_only_page_advances_to_portrait_page(self):
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
            plugin.client = FakePixivClient(
                {
                    0: [
                        _illust(i, width=1600, height=900)
                        for i in range(20)
                    ],
                    20: [_illust(20)],
                }
            )

            try:
                background = await plugin._download_checkin_pixiv_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertIsNotNone(background)
                self.assertEqual(background.illust_id, "20")
                self.assertEqual(plugin.client.ranking_offsets, [0, 20])
                self.assertEqual(plugin.downloader.illust_ids, ["20"])
            finally:
                plugin.image_index.close()

    async def test_all_landscape_pages_use_fallback_without_downloading(self):
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
            plugin.client = FakePixivClient(
                {
                    offset: [
                        _illust(offset + i, width=1600, height=900)
                        for i in range(20)
                    ]
                    for offset in (0, 20, 40, 60, 80)
                }
            )

            try:
                background = await plugin._prepare_checkin_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertEqual(background.mode, "fallback")
                self.assertEqual(plugin.client.ranking_offsets, [0, 20, 40, 60, 80])
                self.assertEqual(plugin.downloader.illust_ids, [])
            finally:
                plugin.image_index.close()


if __name__ == "__main__":
    unittest.main()
