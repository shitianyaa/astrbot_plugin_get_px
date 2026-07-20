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

    async def download(self, url: str, timeout: float) -> tuple[str, int]:
        assert url == "https://example.test/thumb.jpg"
        assert timeout == 30.0
        path = self.root / "downloaded-thumb.jpg"
        content = b"fake-jpeg-thumbnail"
        path.write_bytes(content)
        return str(path), len(content)


@pytest.mark.asyncio
async def test_management_overview_omits_legacy_cleanup_stats() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = build_plugin(tmp)
        api = PluginWebApi(
            plugin,
            plugin_name="astrbot_plugin_get_px",
            log_prefix="[GetPx]",
            internal_error_message="internal",
        )
        app = Quart(__name__)
        app.add_url_rule("/overview", view_func=api.overview, methods=["GET"])
        try:
            async with app.test_app():
                response = await app.test_client().get("/overview")
                payload = await response.get_json()

            assert response.status_code == 200
            assert payload["success"]
            assert "cache_cleanup" not in payload
        finally:
            plugin.image_index.close()


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
async def test_management_api_rejects_non_object_json_payloads() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = build_plugin(tmp)
        api = PluginWebApi(
            plugin,
            plugin_name="astrbot_plugin_get_px",
            log_prefix="[GetPx]",
            internal_error_message="internal",
        )
        app = Quart(__name__)
        routes = {
            "/term/add": api.content_safety_term_add,
            "/term/remove": api.content_safety_term_remove,
            "/blacklist/add": api.image_blacklist_add,
            "/blacklist/remove": api.image_blacklist_remove,
            "/thumbs": api.image_blacklist_thumb_data_batch,
        }
        for path, handler in routes.items():
            app.add_url_rule(path, view_func=handler, methods=["POST"])
        try:
            async with app.test_app():
                client = app.test_client()
                responses = [
                    await client.post(path, json=["invalid"]) for path in routes
                ]
                payloads = [await response.get_json() for response in responses]

            assert all(response.status_code == 400 for response in responses)
            assert all(payload["error"] == "请求内容必须是对象" for payload in payloads)
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


@pytest.mark.asyncio
async def test_management_api_lists_and_updates_checkin_members() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        plugin = build_plugin(tmp)
        await plugin.checkin_store.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
        )
        api = PluginWebApi(
            plugin,
            plugin_name="astrbot_plugin_get_px",
            log_prefix="[GetPx]",
            internal_error_message="internal",
        )
        app = Quart(__name__)
        app.add_url_rule("/members", view_func=api.checkin_members, methods=["GET"])
        app.add_url_rule(
            "/members/update",
            view_func=api.checkin_member_update,
            methods=["POST"],
        )
        try:
            async with app.test_app():
                client = app.test_client()
                listed_response = await client.get("/members?query=Alice&limit=10")
                listed = await listed_response.get_json()
                updated_response = await client.post(
                    "/members/update",
                    json={
                        "user_id": "10001",
                        "coins": 800,
                        "affection": 66.6,
                        "total_days": 20,
                        "streak_days": 7,
                    },
                )
                updated = await updated_response.get_json()
                invalid_response = await client.post(
                    "/members/update",
                    json={
                        "user_id": "10001",
                        "coins": 800,
                        "affection": 66.6,
                        "total_days": 2,
                        "streak_days": 7,
                    },
                )
                missing_response = await client.post(
                    "/members/update",
                    json={
                        "user_id": "404",
                        "coins": 0,
                        "affection": 0,
                        "total_days": 0,
                        "streak_days": 0,
                    },
                )

            assert listed_response.status_code == 200
            assert listed["total"] == 1
            assert listed["members"][0]["username"] == "Alice"
            assert updated_response.status_code == 200
            assert updated["member"]["coins"] == 800
            assert updated["member"]["streak_days"] == 7
            assert invalid_response.status_code == 400
            assert missing_response.status_code == 404
        finally:
            plugin.image_index.close()
