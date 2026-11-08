from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart

from checkin import CheckinStore
from pixiv.index import ImageIndexStore
from plugin_api.api import PluginWebApi
from group_safety import CONFIG_KEY, IDS_KEY, TERMS_KEY, SessionSafetyService


class Config(dict):
    fail = False

    def save_config(self):
        if self.fail:
            raise OSError("save failed")


def build_plugin(tmp: str):
    plugin = SimpleNamespace(
        data_dir=Path(tmp),
        checkin_store=CheckinStore(tmp),
        image_index=ImageIndexStore(tmp),
        client=None,
        downloader=None,
    )
    plugin.config = Config(content_dedupe={})
    plugin.safety_service = SessionSafetyService(plugin.config)
    plugin.safety_service.data_dir = Path(tmp)
    plugin._cfg_str = lambda key, default="": default
    plugin._cfg_float = lambda key, default, lo, hi: default
    return plugin


@pytest.fixture
def api_harness(tmp_path):
    plugin = build_plugin(str(tmp_path))
    api = PluginWebApi(
        plugin, plugin_name="x", log_prefix="[x]", internal_error_message="internal"
    )
    app = Quart(__name__)
    for path, handler, methods in (
        ("/safety", api.content_safety, ["GET"]),
        ("/global", api.content_safety_global, ["POST"]),
        ("/group", api.content_safety_group_save, ["POST"]),
        ("/remove", api.content_safety_group_remove, ["POST"]),
        ("/term", api.content_safety_term_add, ["POST"]),
        ("/blacklist", api.image_blacklist, ["GET"]),
        ("/blacklist", api.image_blacklist_add, ["POST"]),
        ("/blacklist/remove", api.image_blacklist_remove, ["POST"]),
        ("/export", api.data_export, ["GET"]),
        ("/ranking", api.checkin_ranking, ["GET"]),
        ("/thumbs", api.image_blacklist_thumb_data_batch, ["POST"]),
        ("/term/remove", api.content_safety_term_remove, ["POST"]),
    ):
        app.add_url_rule(path, view_func=handler, methods=methods)
    yield plugin, api, app.test_client()
    plugin.image_index.close()


@pytest.mark.asyncio
async def test_safety_crud_global_and_schema_changes_share_one_source(api_harness):
    plugin, _, client = api_harness
    response = await client.post(
        "/group",
        json={
            "name": "插画",
            "sessions": ["bot:GroupMessage:1"],
            "allow_r18": True,
            "custom_terms": ["local"],
        },
    )
    assert response.status_code == 200
    group = (await response.get_json())["group"]
    assert (
        await client.post(
            "/global",
            json={
                TERMS_KEY: ["global"],
                IDS_KEY: ["001"],
                "keyword_match_mode": "严格",
            },
        )
    ).status_code == 200
    plugin.config["content_dedupe"][TERMS_KEY] = ["schema"]
    payload = await (await client.get("/safety")).get_json()
    assert payload[TERMS_KEY] == ["schema"] and payload[IDS_KEY] == ["1"]
    assert len(payload[CONFIG_KEY]) == 1
    conflict = await client.post(
        "/group", json={"name": "other", "sessions": ["bot:GroupMessage:1"]}
    )
    assert conflict.status_code == 400
    before = deepcopy(plugin.config)
    plugin.config.fail = True
    assert (
        await client.post(
            "/group", json={"group_id": group["group_id"], "allow_r18": False}
        )
    ).status_code == 500
    assert plugin.config == before
    plugin.config.fail = False
    assert (
        await client.post("/remove", json={"group_id": group["group_id"]})
    ).status_code == 200
    assert (
        await plugin.safety_service.resolve("bot:GroupMessage:1")
    ).general_only_enabled


@pytest.mark.asyncio
async def test_blacklist_schema_add_remove_and_pages_metadata_share_membership(
    api_harness,
):
    plugin, _, client = api_harness
    plugin.config["content_dedupe"][IDS_KEY] = ["123"]
    payload = await (await client.get("/blacklist")).get_json()
    assert payload["records"] == [{"illust_id": "123"}]
    assert (
        await client.post("/blacklist", json={"illust_id": "456", "reason": "manual"})
    ).status_code == 200
    assert set(plugin.config["content_dedupe"][IDS_KEY]) == {"123", "456"}
    assert (
        await client.post("/blacklist/remove", json={"illust_id": "123"})
    ).status_code == 200
    assert plugin.config["content_dedupe"][IDS_KEY] == ["456"]
    plugin.config["content_dedupe"][IDS_KEY] = []
    assert (await (await client.get("/blacklist")).get_json())["records"] == []


@pytest.mark.asyncio
async def test_safety_api_validation_and_unavailable(api_harness):
    plugin, _, client = api_harness
    for path, payload in (
        ("/global", {IDS_KEY: ["0"]}),
        ("/group", {"name": "x", "sessions": ["1"]}),
        ("/group", {"name": "x", "allow_r18": 1}),
    ):
        assert (await client.post(path, json=payload)).status_code == 400
    for path in (
        "/global",
        "/group",
        "/remove",
        "/term",
        "/term/remove",
        "/blacklist",
        "/blacklist/remove",
        "/thumbs",
    ):
        assert (await client.post(path, json=[])).status_code == 400
    for illust_id in ("invalid", "0", "²", "１２３"):
        assert (
            await client.post("/blacklist", json={"illust_id": illust_id})
        ).status_code == 400
    assert (await client.get("/ranking?group_id=404&type=unknown")).status_code == 400
    plugin.safety_service = None
    assert (await client.get("/safety")).status_code == 503
    assert (await client.post("/group", json={})).status_code == 503


@pytest.mark.asyncio
async def test_data_export_includes_archives_not_credentials(api_harness):
    plugin, _, client = api_harness
    plugin.config["pixiv_source"] = {"pixiv_refresh_token": "must-not-export"}
    plugin.safety_service._write_archive({"config": {"old": "kept"}})
    response = await client.get("/export")
    assert response.status_code == 200
    import json

    raw = await response.get_data()
    data = json.loads(raw)
    assert data["kind"] == "plugin_data_export"
    assert data["checkin"]["users"] == []
    assert data["legacy_safety_archives"][0]["legacy"]["config"]["old"] == "kept"
    assert b"must-not-export" not in raw


class FakePixivClient:
    async def illust_detail(self, illust_id):
        return {
            "id": illust_id,
            "title": "safe",
            "user": {"name": "artist"},
            "x_restrict": 0,
            "tags": [],
            "image_urls": {"medium": "https://example.test/thumb.jpg"},
        }


class FakeDownloader:
    def __init__(self, root):
        self.root = root

    async def download(self, url, timeout):
        path = self.root / "thumb.jpg"
        path.write_bytes(b"fake-jpeg")
        return str(path), 9


@pytest.mark.asyncio
async def test_manual_blacklist_retains_metadata_and_safe_thumbnail(api_harness):
    plugin, _, client = api_harness
    plugin.client = FakePixivClient()
    plugin.downloader = FakeDownloader(plugin.data_dir)
    response = await client.post(
        "/blacklist", json={"illust_id": "00123", "reason": "test"}
    )
    assert response.status_code == 200
    data = await response.get_json()
    assert data["record"]["author"] == "artist"
    assert data["record"]["illust_id"] == "123"
    assert "123" in await plugin.image_index.get_blacklist_thumbnail_paths(["123"])
    assert not (plugin.data_dir / "thumb.jpg").exists()
    from unittest.mock import AsyncMock

    original_add = plugin.image_index.add_blacklist_illust
    plugin.image_index.add_blacklist_illust = AsyncMock(
        side_effect=OSError("metadata failed")
    )
    response = await client.post("/blacklist", json={"illust_id": "456"})
    assert response.status_code == 200 and (await response.get_json())["warning"]
    assert "456" in plugin.config["content_dedupe"][IDS_KEY]
    plugin.image_index.add_blacklist_illust = original_add
    plugin.client.illust_detail = AsyncMock(return_value={"id": 789, "x_restrict": 1})
    plugin.downloader.download = AsyncMock()
    assert (
        await client.post("/blacklist", json={"illust_id": "789"})
    ).status_code == 200
    plugin.downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_management_api_lists_and_updates_checkin_members(tmp_path):
    plugin = build_plugin(str(tmp_path))
    await plugin.checkin_store.checkin(user_id="1", username="Alice", bot_name="neko")
    api = PluginWebApi(
        plugin, plugin_name="x", log_prefix="[x]", internal_error_message="internal"
    )
    app = Quart(__name__)
    app.add_url_rule("/members", view_func=api.checkin_members, methods=["GET"])
    app.add_url_rule(
        "/members/update", view_func=api.checkin_member_update, methods=["POST"]
    )
    try:
        client = app.test_client()
        assert (await (await client.get("/members?query=Alice")).get_json())[
            "total"
        ] == 1
        body = {
            "user_id": "1",
            "coins": 800,
            "affection": 66.6,
            "total_days": 20,
            "streak_days": 7,
        }
        response = await client.post("/members/update", json=body)
        assert (
            response.status_code == 200
            and (await response.get_json())["member"]["coins"] == 800
        )
        assert (
            await client.post("/members/update", json={**body, "total_days": 2})
        ).status_code == 400
        assert (
            await client.post("/members/update", json={**body, "user_id": "404"})
        ).status_code == 404
    finally:
        plugin.image_index.close()


def test_management_api_unregisters_only_owned_routes():
    context = SimpleNamespace(registered_web_apis=[])
    context.register_web_api = lambda route, handler, methods, desc: (
        context.registered_web_apis.append((route, handler, methods, desc))
    )
    api = PluginWebApi(
        SimpleNamespace(context=context),
        plugin_name="x",
        log_prefix="[x]",
        internal_error_message="internal",
    )
    api.register()
    foreign = ("/foreign", object(), ["GET"], "foreign")
    context.registered_web_apis.append(foreign)
    api.unregister()
    assert context.registered_web_apis == [foreign]
