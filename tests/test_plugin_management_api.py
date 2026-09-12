from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart

from checkin import CheckinStore
from pixiv.index import ImageIndexStore
from plugin_api.api import PluginWebApi
from group_safety import CONFIG_KEY, MIGRATION_KEY, GroupSafetyService


def build_plugin(tmp: str):
    plugin = SimpleNamespace()
    plugin.data_dir = Path(tmp)
    plugin.checkin_store = CheckinStore(tmp)
    class Config(dict):
        def save_config(self): pass
    plugin.config = Config(group_content_safety_policies=[], group_content_safety_policies_migrated=True)
    plugin.group_safety_service = GroupSafetyService(plugin.config)
    plugin.image_index = ImageIndexStore(tmp)
    plugin.client = None
    plugin.downloader = None
    plugin._cfg_str = lambda key, default="": default
    plugin._cfg_float = lambda key, default, lo, hi: default
    return plugin


class FailNextSaveConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_next = False

    def save_config(self):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("save failed")


class GroupPolicyHarness:
    def __init__(self, tmp: str):
        self.plugin = build_plugin(tmp)
        self.plugin.config = FailNextSaveConfig(
            group_content_safety_policies=[], group_content_safety_policies_migrated=True
        )
        self.plugin.group_safety_service = GroupSafetyService(self.plugin.config)
        self.api = PluginWebApi(self.plugin, plugin_name="x", log_prefix="[x]", internal_error_message="internal")
        self.app = Quart(__name__)
        self.app.add_url_rule("/content-safety", view_func=self.api.content_safety, methods=["GET"])
        self.app.add_url_rule("/content-safety/group-policy", view_func=self.api.content_safety_group_policy, methods=["POST"])
        self.app.add_url_rule("/content-safety/group-policies", view_func=self.api.content_safety_group_policies, methods=["GET"])
        self.app.add_url_rule("/content-safety/group-policy/remove", view_func=self.api.content_safety_group_policy_remove, methods=["POST"])
        self.app.add_url_rule("/content-safety/private-policy", view_func=self.api.content_safety_private_policy, methods=["POST"])
        self.app.add_url_rule("/content-safety/private-policies", view_func=self.api.content_safety_private_policies, methods=["GET"])
        self.app.add_url_rule("/content-safety/private-policy/remove", view_func=self.api.content_safety_private_policy_remove, methods=["POST"])
        self.app.add_url_rule("/content-safety/policies/apply-field", view_func=self.api.content_safety_policy_apply_field, methods=["POST"])


@pytest.fixture
def group_policy_harness():
    with tempfile.TemporaryDirectory() as tmp:
        harness = GroupPolicyHarness(tmp)
        try:
            yield harness
        finally:
            harness.plugin.image_index.close()


@pytest.mark.asyncio
async def test_apply_field_quart_contract(group_policy_harness):
    h = group_policy_harness
    await h.plugin.group_safety_service.upsert_group_policy("g1", general_only_enabled=True, builtin_terms_enabled=False, custom_terms=[" Alpha "], blacklisted_illust_ids=["001"])
    client = h.app.test_client()
    response = await client.post("/content-safety/policies/apply-field", json={"source_scope":"group","source_id":"g1","field":"custom_terms","target":"group"})
    assert response.status_code == 200
    assert (await response.get_json())["updated_count"] == 0

@pytest.mark.asyncio
async def test_policy_crud_round_trip_lists(group_policy_harness):
    c=group_policy_harness.app.test_client()
    r=await c.post("/content-safety/group-policy",json={"group_id":"g2","general_only_enabled":True,"builtin_terms_enabled":False,"custom_terms":["x"],"blacklisted_illust_ids":["001"]})
    assert r.status_code==200
    assert (await r.get_json())["group_policy"]["custom_terms"]==["x"]

@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint,identifier", [("/content-safety/group-policy", "group_id"), ("/content-safety/private-policy", "user_id")])
@pytest.mark.parametrize("field", ["custom_terms", "blacklisted_illust_ids"])
async def test_policy_api_requires_arrays_when_list_fields_are_present(group_policy_harness, endpoint, identifier, field):
    payload = {identifier: "target", "general_only_enabled": True, "builtin_terms_enabled": True, field: None}
    response = await group_policy_harness.app.test_client().post(endpoint, json=payload)
    assert response.status_code == 400
    assert field in (await response.get_json())["error"]

@pytest.mark.asyncio
async def test_apply_field_targets_all_existing(group_policy_harness):
    s=group_policy_harness.plugin.group_safety_service
    await s.upsert_group_policy("g1",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=["x"],blacklisted_illust_ids=[])
    await s.upsert_private_policy("u1",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=[],blacklisted_illust_ids=[])
    r=await group_policy_harness.app.test_client().post("/content-safety/policies/apply-field",json={"source_scope":"group","source_id":"g1","field":"custom_terms","target":"all"})
    assert r.status_code==200 and (await r.get_json())["updated_count"]==1

@pytest.mark.asyncio
async def test_apply_field_all_save_failure_returns_500_and_restores_both_scopes(group_policy_harness):
    service = group_policy_harness.plugin.group_safety_service
    await service.upsert_group_policy("g1", general_only_enabled=True, builtin_terms_enabled=True, custom_terms=["source"], blacklisted_illust_ids=["1"])
    await service.upsert_private_policy("u1", general_only_enabled=False, builtin_terms_enabled=False, custom_terms=["old"], blacklisted_illust_ids=["2"])
    before_group = await service.list_policies()
    before_private = await service.list_private_policies()
    before_config = {k: [dict(x) for x in group_policy_harness.plugin.config.get(k, [])] for k in ("group_content_safety_policies", "private_content_safety_policies")}
    group_policy_harness.plugin.config.fail_next = True
    response = await group_policy_harness.app.test_client().post("/content-safety/policies/apply-field", json={"source_scope":"group", "source_id":"g1", "field":"custom_terms", "target":"all"})
    assert response.status_code == 500
    assert await service.list_policies() == before_group and await service.list_private_policies() == before_private
    assert {k: [dict(x) for x in group_policy_harness.plugin.config.get(k, [])] for k in before_config} == before_config

@pytest.mark.asyncio
async def test_apply_field_bad_source_returns_404(group_policy_harness):
    r=await group_policy_harness.app.test_client().post("/content-safety/policies/apply-field",json={"source_scope":"group","source_id":"missing","field":"custom_terms","target":"all"})
    assert r.status_code==404

@pytest.mark.asyncio
async def test_apply_field_invalid_returns_400(group_policy_harness):
    r=await group_policy_harness.app.test_client().post("/content-safety/policies/apply-field",json={"source_scope":"x","source_id":"1","field":"custom_terms","target":"all"})
    assert r.status_code==400

@pytest.mark.asyncio
async def test_policy_service_unavailable_returns_503(group_policy_harness):
    group_policy_harness.plugin.group_safety_service=None
    r=await group_policy_harness.app.test_client().post("/content-safety/policies/apply-field",json={})
    assert r.status_code==503


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
        payload = b"fake-jpeg-thumbnail"
        path.write_bytes(payload)
        return str(path), len(payload)

@pytest.mark.asyncio
async def test_group_policy_list_is_fully_sorted_and_retains_explicit_strict(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        await client.post("/content-safety/group-policy", json={"group_id": "2", "general_only_enabled": True, "builtin_terms_enabled": True})
        await client.post("/content-safety/group-policy", json={"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": True})
        response = await client.get("/content-safety/group-policies")
        payload = await response.get_json()
    assert response.status_code == 200
    assert [p["group_id"] for p in payload["group_policies"]] == ["1", "2"]
    assert all(p["is_default"] is False for p in payload["group_policies"])
    assert [item["group_id"] for item in group_policy_harness.plugin.config[CONFIG_KEY]] == ["1", "2"]
    assert all(item["updated_by"] == "web" and item["updated_at"] for item in group_policy_harness.plugin.config[CONFIG_KEY])

@pytest.mark.asyncio
async def test_update_replaces_existing_without_duplicate(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        await client.post("/content-safety/group-policy", json={"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": True})
        response = await client.post("/content-safety/group-policy", json={"group_id": "1", "general_only_enabled": False, "builtin_terms_enabled": False})
        listed = await (await client.get("/content-safety/group-policies")).get_json()
    assert response.status_code == 200
    assert len(listed["group_policies"]) == 1
    assert listed["group_policies"][0]["general_only_enabled"] is False
    assert listed["group_policies"][0]["builtin_terms_enabled"] is False
    response_payload = await response.get_json()
    assert response_payload["group_policy"]["group_id"] == "1"
    assert response_payload["group_policy"]["general_only_enabled"] is False
    assert response_payload["group_policy"]["builtin_terms_enabled"] is False
    assert listed["group_policies"][0]["group_id"] == "1"
    assert len(group_policy_harness.plugin.config[CONFIG_KEY]) == 1
    assert group_policy_harness.plugin.config[CONFIG_KEY][0]["group_id"] == "1"
    assert group_policy_harness.plugin.config[CONFIG_KEY][0]["general_only_enabled"] is False
    assert group_policy_harness.plugin.config[CONFIG_KEY][0]["builtin_terms_enabled"] is False
    service_policy = await group_policy_harness.plugin.group_safety_service.list_policies()
    assert len(service_policy) == 1 and service_policy[0]["group_id"] == "1"
    assert service_policy[0]["general_only_enabled"] is False and service_policy[0]["builtin_terms_enabled"] is False
    assert group_policy_harness.plugin.config[MIGRATION_KEY] is True

@pytest.mark.asyncio
async def test_remove_returns_strict_default_and_is_idempotent(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        await client.post("/content-safety/group-policy", json={"group_id": "1", "general_only_enabled": False, "builtin_terms_enabled": False})
        first = await client.post("/content-safety/group-policy/remove", json={"group_id": "1"})
        first_payload = await first.get_json()
        second = await client.post("/content-safety/group-policy/remove", json={"group_id": "1"})
        second_payload = await second.get_json()
        lookup = await client.get("/content-safety?group_id=1")
        lookup_payload = await lookup.get_json()
    assert first.status_code == 200 and first_payload["removed"] is True
    assert first_payload["group_policy"]["is_default"] is True
    assert first_payload["group_policy"]["general_only_enabled"] is True and first_payload["group_policy"]["builtin_terms_enabled"] is True
    assert second.status_code == 200 and second_payload["removed"] is False
    assert lookup.status_code == 200 and lookup_payload["group_policy"]["is_default"] is True
    assert not group_policy_harness.plugin.config[CONFIG_KEY]
    assert not await group_policy_harness.plugin.group_safety_service.list_policies()

@pytest.mark.parametrize("payload", [None, [], {}, {"group_id": None, "general_only_enabled": True, "builtin_terms_enabled": True}, {"group_id": "1"}, {"group_id": "1", "general_only_enabled": True}, {"group_id": "1", "builtin_terms_enabled": True}, {"group_id": 1, "general_only_enabled": True, "builtin_terms_enabled": True}, {"group_id": " ", "general_only_enabled": True, "builtin_terms_enabled": True}, {"group_id": "a" * 129, "general_only_enabled": True, "builtin_terms_enabled": True}, {"group_id": "a\x00", "general_only_enabled": True, "builtin_terms_enabled": True}, {"group_id": "1", "general_only_enabled": None, "builtin_terms_enabled": True}, {"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": None}, {"group_id": "1", "general_only_enabled": 1, "builtin_terms_enabled": True}, {"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": "yes"}])
@pytest.mark.asyncio
async def test_upsert_rejects_invalid_payloads(group_policy_harness, payload):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.post("/content-safety/group-policy", json=payload)
        listed = await (await client.get("/content-safety/group-policies")).get_json()
    assert response.status_code == 400
    assert (await response.get_json())["success"] is False
    assert listed["group_policies"] == []

@pytest.mark.parametrize("payload", [None, [], {}, {"group_id": None}, {"group_id": 1}, {"group_id": " "}, {"group_id": "a" * 129}, {"group_id": "a\x00"}])
@pytest.mark.asyncio
async def test_remove_rejects_invalid_payloads(group_policy_harness, payload):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.post("/content-safety/group-policy/remove", json=payload)
        listed = await (await client.get("/content-safety/group-policies")).get_json()
    assert response.status_code == 400
    assert (await response.get_json())["success"] is False
    assert listed["group_policies"] == []

@pytest.mark.asyncio
async def test_lookup_returns_503_without_service(group_policy_harness):
    group_policy_harness.plugin.group_safety_service = None
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.get("/content-safety?group_id=1")
        payload = await response.get_json()
    assert response.status_code == 503
    assert payload["success"] is False and payload["error"]

@pytest.mark.asyncio
async def test_upsert_returns_503_without_service(group_policy_harness):
    group_policy_harness.plugin.group_safety_service = None
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.post("/content-safety/group-policy", json={})
        payload = await response.get_json()
    assert response.status_code == 503
    assert payload["success"] is False and payload["error"]

@pytest.mark.asyncio
async def test_list_returns_503_without_service(group_policy_harness):
    group_policy_harness.plugin.group_safety_service = None
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.get("/content-safety/group-policies")
        payload = await response.get_json()
    assert response.status_code == 503
    assert payload["success"] is False and payload["error"]

@pytest.mark.asyncio
async def test_remove_returns_503_without_service(group_policy_harness):
    group_policy_harness.plugin.group_safety_service = None
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.post("/content-safety/group-policy/remove", json={})
        payload = await response.get_json()
    assert response.status_code == 503
    assert payload["success"] is False and payload["error"]

@pytest.mark.asyncio
async def test_global_content_safety_remains_200_without_group_service(group_policy_harness):
    group_policy_harness.plugin.group_safety_service = None
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        response = await client.get("/content-safety")
        payload = await response.get_json()
    assert response.status_code == 200
    assert payload["success"] is True and payload["rating_policy"] == "general_only"

@pytest.mark.asyncio
async def test_upsert_save_failure_rolls_back_config_and_runtime(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        await client.post("/content-safety/group-policy", json={"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": True})
        old = group_policy_harness.plugin.config[CONFIG_KEY]
        snapshot = [dict(x) for x in old]
        service_snapshot = await group_policy_harness.plugin.group_safety_service.list_policies()
        marker = group_policy_harness.plugin.config[MIGRATION_KEY]
        group_policy_harness.plugin.config.fail_next = True
        response = await client.post("/content-safety/group-policy", json={"group_id": "2", "general_only_enabled": False, "builtin_terms_enabled": False})
        listed = await (await client.get("/content-safety/group-policies")).get_json()
    assert response.status_code == 500
    assert group_policy_harness.plugin.config[CONFIG_KEY] is old and group_policy_harness.plugin.config[CONFIG_KEY] == snapshot
    assert group_policy_harness.plugin.config[MIGRATION_KEY] is marker
    assert [p["group_id"] for p in listed["group_policies"]] == ["1"]
    assert await group_policy_harness.plugin.group_safety_service.list_policies() == service_snapshot

@pytest.mark.asyncio
async def test_remove_save_failure_rolls_back_config_and_runtime(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        await client.post("/content-safety/group-policy", json={"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": True})
        old = group_policy_harness.plugin.config[CONFIG_KEY]
        snapshot = [dict(x) for x in old]
        service_snapshot = await group_policy_harness.plugin.group_safety_service.list_policies()
        marker = group_policy_harness.plugin.config[MIGRATION_KEY]
        group_policy_harness.plugin.config.fail_next = True
        response = await client.post("/content-safety/group-policy/remove", json={"group_id": "1"})
        listed = await (await client.get("/content-safety/group-policies")).get_json()
    assert response.status_code == 500
    assert group_policy_harness.plugin.config[CONFIG_KEY] is old and group_policy_harness.plugin.config[CONFIG_KEY] == snapshot
    assert group_policy_harness.plugin.config[MIGRATION_KEY] is marker
    assert [p["group_id"] for p in listed["group_policies"]] == ["1"]
    assert await group_policy_harness.plugin.group_safety_service.list_policies() == service_snapshot


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
async def test_management_api_group_safety_policy_round_trip_and_validation() -> None:
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
            "/group-policy",
            view_func=api.content_safety_group_policy,
            methods=["POST"],
        )
        try:
            async with app.test_app():
                client = app.test_client()
                default = await (await client.get("/safety?group_id=unknown")).get_json()
                updated_response = await client.post(
                    "/group-policy",
                    json={
                        "group_id": "unknown",
                        "general_only_enabled": False,
                        "builtin_terms_enabled": True,
                    },
                )
                updated = await updated_response.get_json()
                fetched = await (await client.get("/safety?group_id=unknown")).get_json()
                invalid = await client.post(
                    "/group-policy",
                    json={
                        "group_id": "unknown",
                        "general_only_enabled": "false",
                        "builtin_terms_enabled": True,
                    },
                )
                invalid_group_id = await client.post(
                    "/group-policy",
                    json={
                        "group_id": [],
                        "general_only_enabled": False,
                        "builtin_terms_enabled": True,
                    },
                )

            assert default["group_policy"]["is_default"] is True
            assert updated_response.status_code == 200
            assert updated["group_policy"]["general_only_enabled"] is False
            assert fetched["rating_policy"] == "allow_sensitive"
            assert invalid.status_code == 400
            assert invalid_group_id.status_code == 400
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


def test_management_api_unregisters_only_owned_routes() -> None:
    context = SimpleNamespace(registered_web_apis=[])

    def register_web_api(
        route: str, handler: object, methods: list[str], description: str
    ) -> None:
        context.registered_web_apis.append((route, handler, methods, description))

    context.register_web_api = register_web_api
    plugin = SimpleNamespace(context=context)
    api = PluginWebApi(
        plugin,
        plugin_name="astrbot_plugin_get_px",
        log_prefix="[GetPx]",
        internal_error_message="internal",
    )

    api.register()
    foreign_handler = object()
    foreign_registration = ("/foreign", foreign_handler, ["GET"], "foreign")
    context.registered_web_apis.append(foreign_registration)

    api.unregister()

    assert context.registered_web_apis == [foreign_registration]
    api.unregister()
    assert context.registered_web_apis == [foreign_registration]

@pytest.mark.asyncio
async def test_private_policy_query_response_uses_private_field(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client=group_policy_harness.app.test_client()
        response=await client.post("/content-safety/private-policy",json={"user_id":"u-test","general_only_enabled":False,"builtin_terms_enabled":True})
        assert response.status_code==200
        response=await client.get("/content-safety",query_string={"user_id":"u-test"})
        payload=await response.get_json()
    assert response.status_code==200 and payload["private_policy"]["user_id"]=="u-test"


@pytest.mark.asyncio
async def test_private_policy_crud_is_persistent_and_idempotent(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        created = await client.post(
            "/content-safety/private-policy",
            json={"user_id": " u1 ", "general_only_enabled": False, "builtin_terms_enabled": True},
        )
        updated = await client.post(
            "/content-safety/private-policy",
            json={"user_id": "u1", "general_only_enabled": True, "builtin_terms_enabled": False},
        )
        listed = await (await client.get("/content-safety/private-policies")).get_json()
        removed = await (await client.post("/content-safety/private-policy/remove", json={"user_id": "u1"})).get_json()
        removed_again = await (await client.post("/content-safety/private-policy/remove", json={"user_id": "u1"})).get_json()

    assert created.status_code == updated.status_code == 200
    assert len(listed["private_policies"]) == 1
    assert listed["private_policies"][0]["builtin_terms_enabled"] is False
    assert removed["removed"] is True
    assert removed["private_policy"]["is_default"] is True
    assert removed_again["removed"] is False


@pytest.mark.asyncio
async def test_private_policy_validation_conflict_and_unavailable(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        invalid_bodies = [
            {},
            {"user_id": "", "general_only_enabled": True, "builtin_terms_enabled": True},
            {"user_id": "u1", "general_only_enabled": 1, "builtin_terms_enabled": True},
            {"user_id": "u\n1", "general_only_enabled": True, "builtin_terms_enabled": True},
        ]
        responses = [await client.post("/content-safety/private-policy", json=body) for body in invalid_bodies]
        conflict = await client.get("/content-safety?group_id=&user_id=")
        group_policy_harness.plugin.group_safety_service = None
        unavailable = await client.get("/content-safety/private-policies")

    assert all(response.status_code == 400 for response in responses)
    assert conflict.status_code == 400
    assert unavailable.status_code == 503


@pytest.mark.asyncio
async def test_private_policy_save_failure_rolls_back(group_policy_harness):
    async with group_policy_harness.app.test_app():
        client = group_policy_harness.app.test_client()
        await client.post(
            "/content-safety/private-policy",
            json={"user_id": "u1", "general_only_enabled": False, "builtin_terms_enabled": True},
        )
        before_config = [dict(item) for item in group_policy_harness.plugin.config["private_content_safety_policies"]]
        before_runtime = await group_policy_harness.plugin.group_safety_service.list_private_policies()
        group_policy_harness.plugin.config.fail_next = True
        failed = await client.post(
            "/content-safety/private-policy",
            json={"user_id": "u1", "general_only_enabled": True, "builtin_terms_enabled": False},
        )

    assert failed.status_code == 500
    assert group_policy_harness.plugin.config["private_content_safety_policies"] == before_config
    assert await group_policy_harness.plugin.group_safety_service.list_private_policies() == before_runtime
