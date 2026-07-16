import json
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

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
    def __init__(self, pages, search_pages=None):
        self.pages = pages
        self.search_pages = search_pages or {}
        self.recommended_offsets = []
        self.search_calls = []

    async def recommended(self, offset: int = 0):
        self.recommended_offsets.append(offset)
        return list(self.pages.get(offset, []))

    async def search(self, tag: str, offset: int = 0):
        self.search_calls.append((tag, offset))
        return list(self.search_pages.get((tag, offset), []))


class FakeDownloader:
    def __init__(self):
        self.illust_ids = []
        self.qualities = []
        self.downgrade_limits = []

    async def download_for_send(
        self, illust, quality, proxy, timeout, downgrade_limit_bytes, log_context
    ):
        self.illust_ids.append(str(illust["id"]))
        self.qualities.append(quality)
        self.downgrade_limits.append(downgrade_limit_bytes)
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
    def test_split_config_tags_accepts_common_delimiters(self):
        self.assertEqual(
            GetPxPlugin._split_config_tags(
                "alpha\uff0cbeta、gamma;delta\uff1bepsilon\nzeta"
            ),
            ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"],
        )

    def test_checkin_background_tag_candidates_are_randomized(self):
        plugin = object.__new__(GetPxPlugin)
        with patch(
            "astrbot_plugin_get_px.checkin.artwork.random.shuffle",
            side_effect=lambda tags: tags.reverse(),
        ):
            candidates = plugin._checkin_background_tag_candidates("alpha,beta,gamma")
        self.assertEqual(candidates, ["gamma", "beta", "alpha"])

    def test_greeting_schema_defaults_to_hitokoto_without_legacy_switch(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertNotIn("checkin_ai_greeting_enabled", schema)
        self.assertEqual(schema["checkin_greeting_mode"]["default"], "hitokoto")
        self.assertEqual(
            schema["checkin_greeting_mode"]["options"],
            ["local", "hitokoto", "ai"],
        )
        categories = schema["checkin_hitokoto_categories"]
        self.assertEqual(categories["type"], "list")
        self.assertEqual(categories["default"], ["全部"])
        self.assertIn("动画", categories["options"])
        self.assertIn("诗词", categories["options"])

    def test_economy_and_dedupe_schema_limits_are_bounded(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["dedupe_ttl_hours"]["slider"]["max"], 24)
        self.assertEqual(
            schema["checkin_background_refresh_cost"]["slider"]["max"], 500
        )

    def test_pixiv_ai_comment_settings_are_removed(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        for key in (
            "ai_enabled",
            "ai_probability",
            "ai_max_images",
            "ai_pre_message",
            "ai_vision_provider_id",
            "ai_comment_provider_id",
            "ai_vision_prompt",
            "ai_comment_prompt",
        ):
            self.assertNotIn(key, schema)

    def test_custom_background_schema_recommends_portrait_contain_display(self):
        schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertNotIn("checkin_background_aspect_ratio", schema)
        self.assertNotIn("checkin_background_aspect_tolerance", schema)
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
                    source="pixiv:recommended",
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
                "filter_manga": True,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
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
                        "group:20001", "pixiv:recommended"
                    ),
                    set(),
                )
                self.assertEqual(plugin.client.recommended_offsets, [0, 0, 0])
                self.assertEqual(plugin.downloader.qualities, ["medium"] * 3)
                self.assertEqual(plugin.downloader.downgrade_limits, [0] * 3)
            finally:
                plugin.image_index.close()

    async def test_preview_background_prunes_expired_users(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {"checkin_background_mode": "pixiv_daily"}
        plugin._checkin_preview_background_ids = {
            "expired": [("1", 0.0)],
        }
        plugin._download_checkin_pixiv_background = AsyncMock(
            return_value=CardBackground(mode="fallback", source="fallback")
        )

        await plugin._prepare_checkin_background(
            FakeEvent(),
            SimpleNamespace(date_key="2026-07-12", user_id="10001"),
            claim_usage=False,
            refresh_preview=True,
        )

        self.assertNotIn("expired", plugin._checkin_preview_background_ids)

    async def test_checkin_background_tries_remaining_configured_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "filter_manga": True,
                "checkin_background_tag": "empty\uff0cavailable",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {}, search_pages={("available", 0): [_illust(72)]}
            )
            try:
                with patch(
                    "astrbot_plugin_get_px.checkin.artwork.random.shuffle",
                    side_effect=lambda tags: None,
                ):
                    background = await plugin._download_checkin_pixiv_background(
                        FakeEvent(),
                        SimpleNamespace(date_key="2026-07-12", user_id="10001"),
                        claim_usage=False,
                        preview_nonce=1,
                    )
                self.assertIsNotNone(background)
                self.assertEqual(background.source, "pixiv:search:available")
                self.assertEqual(
                    plugin.client.search_calls,
                    [("empty", 0), ("available", 0)],
                )
            finally:
                plugin.image_index.close()

    async def test_checkin_background_skips_page_used_by_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "filter_manga": True,
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {
                    0: [_illust(i) for i in range(20)],
                    20: [_illust(20), _illust(21)],
                }
            )

            try:
                for illust_id in range(20):
                    await plugin.image_index.record_usage(
                        scope="group:20001",
                        source_key="pixiv:recommended",
                        illust_id=str(illust_id),
                        feature="checkin",
                        user_id="10001",
                    )

                background = await plugin._download_checkin_pixiv_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertIsNotNone(background)
                self.assertEqual(background.illust_id, "20")
                self.assertEqual(plugin.client.recommended_offsets, [0, 20])
            finally:
                plugin.image_index.close()

    async def test_checkin_background_claim_can_be_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "filter_manga": True,
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
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
                        "group:20001", "pixiv:recommended"
                    ),
                    {"30"},
                )

                await plugin._release_checkin_background_claim(FakeEvent(), background)

                self.assertEqual(
                    await plugin.image_index.get_used_illust_ids(
                        "group:20001", "pixiv:recommended"
                    ),
                    set(),
                )
            finally:
                plugin.image_index.close()

    async def test_cancelled_background_download_releases_claim(self):
        class CancelledDownloader(FakeDownloader):
            async def download_for_send(self, *args, **kwargs):
                raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "filter_manga": True,
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.downloader = CancelledDownloader()
            plugin.client = FakePixivClient({0: [_illust(31)]})

            try:
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._download_checkin_pixiv_background(
                        FakeEvent(),
                        SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                    )
                self.assertEqual(
                    await plugin.image_index.get_used_illust_ids(
                        "group:20001", "pixiv:recommended"
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
                "filter_manga": True,
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {
                    0: [_illust(i, width=1600, height=900) for i in range(20)],
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
                self.assertEqual(plugin.client.recommended_offsets, [0, 20])
                self.assertEqual(plugin.downloader.illust_ids, ["20"])
            finally:
                plugin.image_index.close()

    async def test_pixiv_fallback_resumes_from_persisted_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "filter_manga": True,
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {
                    40: [
                        _illust(40 + i, width=1600, height=900)
                        for i in range(20)
                    ],
                    60: [_illust(60)],
                }
            )
            try:
                await plugin.image_index.advance_page_offset(
                    "group:20001", "pixiv:recommended", 20
                )
                await plugin.image_index.advance_page_offset(
                    "group:20001", "pixiv:recommended", 20
                )

                background = await plugin._download_checkin_pixiv_background(
                    FakeEvent(),
                    SimpleNamespace(date_key="2026-05-26", user_id="10001"),
                )

                self.assertIsNotNone(background)
                self.assertEqual(background.illust_id, "60")
                self.assertEqual(plugin.client.recommended_offsets, [40, 60])
            finally:
                plugin.image_index.close()

    async def test_lolicon_background_restore_uses_pixiv_page_when_token_exists(self):
        class DetailClient(FakePixivClient):
            def __init__(self):
                super().__init__({})
                self.detail_calls = []

            async def illust_detail(self, illust_id):
                self.detail_calls.append(illust_id)
                return {
                    "id": illust_id,
                    "title": "title",
                    "x_restrict": 0,
                    "width": 750,
                    "height": 1000,
                    "user": {"name": "artist"},
                    "meta_pages": [
                        {"image_urls": {"original": "page-0.jpg"}},
                        {"image_urls": {"original": "page-1.jpg"}},
                    ],
                }

        class RecordingDownloader(FakeDownloader):
            def __init__(self):
                super().__init__()
                self.last_illust = None

            async def download_for_send(self, illust, *args, **kwargs):
                self.last_illust = illust
                return await super().download_for_send(illust, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.client = DetailClient()
            plugin.downloader = RecordingDownloader()
            record = SimpleNamespace(
                background_mode="pixiv_daily",
                background_source="lolicon:random",
                background_illust_id="123:1",
                background_title="title",
                background_author="artist",
            )
            try:
                background = await plugin._restore_checkin_background(
                    FakeEvent(), record
                )

                self.assertEqual(plugin.client.detail_calls, [123])
                self.assertEqual(
                    plugin.downloader.last_illust["meta_single_page"][
                        "original_image_url"
                    ],
                    "page-1.jpg",
                )
                self.assertEqual(background.illust_id, "123:1")
                self.assertEqual(background.mode, "pixiv_daily")
            finally:
                plugin.image_index.close()

    async def test_all_landscape_pages_use_fallback_without_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "pixiv_refresh_token": "token",
                "filter_manga": True,
                "checkin_background_aspect_ratio": "16:9",
                "checkin_background_aspect_tolerance": 0.25,
                "image_quality": "large",
                "pixiv_proxy_url": "",
                "request_timeout": 30.0,
                "auto_downgrade_original_mb": 3.0,
            }
            plugin.image_index = ImageIndexStore(tmp)
            plugin.downloader = FakeDownloader()
            plugin.client = FakePixivClient(
                {
                    offset: [
                        _illust(offset + i, width=1600, height=900) for i in range(20)
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
                self.assertEqual(plugin.client.recommended_offsets, [0, 20, 40, 60, 80])
                self.assertEqual(plugin.downloader.illust_ids, [])
            finally:
                plugin.image_index.close()


if __name__ == "__main__":
    unittest.main()
