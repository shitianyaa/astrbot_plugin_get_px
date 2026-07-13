from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart

from checkin import CheckinStore
from pixiv.index import ImageIndexStore
from plugin_api.api import PluginWebApi


def build_plugin(tmp: str):
    plugin = SimpleNamespace()
    plugin.data_dir = Path(tmp)
    plugin.checkin_store = CheckinStore(tmp)
    plugin.image_index = ImageIndexStore(tmp)
    plugin.client = None
    plugin.downloader = None
    plugin.cache_cleanup_summary = {
        "cleaned": 2,
        "skipped": 0,
        "failed": 0,
        "files": 4,
        "bytes": 128,
    }
    plugin._cfg_str = lambda key, default="": default
    plugin._cfg_float = lambda key, default, lo, hi: default
    return plugin


class FakePixivClient:
    async def illust_detail(self, illust_id: int):
        return {
            "id": illust_id,
            "title": "安全测试作品",
            "user": {"name": "Test Artist"},
            "x_restrict": 0,
            "tags": [],
            "image_urls": {"square_medium": "https://example.test/thumb.jpg"},
        }


class FakeDownloader:
    def __init__(self, root: Path):
        self.root = root

    async def download(self, url: str, proxy: str, timeout: float) -> str:
        assert url == "https://example.test/thumb.jpg"
        assert proxy == ""
        assert timeout == 30.0
        path = self.root / "downloaded-thumb.jpg"
        path.write_bytes(b"fake-jpeg-thumbnail")
        return str(path)


@pytest.mark.asyncio
async def test_management_api_safety_and_blacklist_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = build_plugin(tmp)
        api = PluginWebApi(
            plugin,
            plugin_name="astrbot_plugin_get_px",
            log_prefix="[GetPx]",
            internal_error_message="internal",
        )
        app = Quart(__name__)
        app.add_url_rule("/safety", view_func=api.content_safety, methods=["GET"])
        app.add_url_rule(
            "/term", view_func=api.content_safety_term_add, methods=["POST"]
        )
        app.add_url_rule(
            "/blacklist", view_func=api.image_blacklist_add, methods=["POST"]
        )
        try:
            async with app.test_app():
                client = app.test_client()
                builtin = await (await client.get("/safety")).get_json()
                added = await (
                    await client.post("/term", json={"term": "危险主题"})
                ).get_json()
                blocked = await (
                    await client.post(
                        "/blacklist",
                        json={"illust_id": "123456", "reason": "不适合作为背景"},
                    )
                ).get_json()

            assert builtin["rating_policy"] == "general_only"
            assert added["success"]
            assert blocked["record"]["reason"] == "不适合作为背景"
            assert await plugin.image_index.is_blacklisted("123456")
        finally:
            plugin.image_index.close()


@pytest.mark.asyncio
async def test_management_api_validates_ranking_and_illustration_ids() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = build_plugin(tmp)
        api = PluginWebApi(
            plugin,
            plugin_name="astrbot_plugin_get_px",
            log_prefix="[GetPx]",
            internal_error_message="internal",
        )
        app = Quart(__name__)
        app.add_url_rule("/ranking", view_func=api.checkin_ranking, methods=["GET"])
        app.add_url_rule(
            "/blacklist", view_func=api.image_blacklist_add, methods=["POST"]
        )
        try:
            async with app.test_app():
                client = app.test_client()
                missing_group_response = await client.get(
                    "/ranking?group_id=404&type=today"
                )
                ranking_response = await client.get(
                    "/ranking?group_id=404&type=unknown"
                )
                blacklist_response = await client.post(
                    "/blacklist", json={"illust_id": "not-a-number"}
                )

            assert missing_group_response.status_code == 400
            assert ranking_response.status_code == 400
            assert blacklist_response.status_code == 400
        finally:
            plugin.image_index.close()


@pytest.mark.asyncio
async def test_manual_blacklist_fetches_metadata_and_safe_thumbnail() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = build_plugin(tmp)
        plugin.client = FakePixivClient()
        plugin.downloader = FakeDownloader(Path(tmp))
        api = PluginWebApi(
            plugin,
            plugin_name="astrbot_plugin_get_px",
            log_prefix="[GetPx]",
            internal_error_message="internal",
        )
        app = Quart(__name__)
        app.add_url_rule(
            "/blacklist", view_func=api.image_blacklist_add, methods=["POST"]
        )
        try:
            async with app.test_app():
                response = await app.test_client().post(
                    "/blacklist", json={"illust_id": "123456", "reason": "测试"}
                )
                result = await response.get_json()

            assert response.status_code == 200
            assert result["record"]["title"] == "安全测试作品"
            assert result["record"]["author"] == "Test Artist"
            assert result["record"]["thumb_id"] == "123456.jpg"
            thumb = await plugin.image_index.get_blacklist_thumbnail_path("123456")
            assert thumb is not None
            assert thumb.read_bytes() == b"fake-jpeg-thumbnail"
            assert not (Path(tmp) / "downloaded-thumb.jpg").exists()
        finally:
            plugin.image_index.close()
