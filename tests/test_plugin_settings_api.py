from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from astrbot.api import AstrBotConfig
from quart import Quart

from plugin_api import settings as settings_module
from plugin_api.api import PluginWebApi
from plugin_api.settings import SAFETY_KEYS, save_settings, settings_snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "host", "family"),
    [
        ("mirror", "cdn-font.hyperos.mi.com", "MiSans"),
        ("official", "cdn.jsdelivr.net", "MiSans VF"),
        ("none", "", ""),
        ("unknown", "", ""),
    ],
)
async def test_font_config_uses_selected_route_or_system_without_saving(
    source, host, family
):
    target = plugin(Config())
    target._cfg_str = lambda key, default="": source
    api = PluginWebApi(
        target, plugin_name="x", log_prefix="[x]", internal_error_message="internal"
    )
    app = Quart(__name__)
    app.add_url_rule("/config", view_func=api.config, methods=["GET"])
    response = await app.test_client().get("/config")
    config = await response.get_json()
    assert config["font_source"] == source
    assert urlparse(config["font_url"]).hostname == (host or None)
    assert config["font_family"] == family
    assert config["font_label"]
    assert target.config.saves == 0
    if source == "official":
        assert "misans@5.0.0/" in config["font_url"]


def test_font_route_labels_keep_existing_saved_values():
    target = plugin(Config(runtime={"webui_font_source": "official"}))
    field = next(
        field
        for group in settings_snapshot(target)["groups"]
        for field in group["fields"]
        if field["key"] == "webui_font_source"
    )
    assert field["value"] == "official"
    assert [option["value"] for option in field["options"]] == [
        "mirror",
        "official",
        "none",
    ]
    assert all(
        option["label"] and option["label"] != option["value"]
        for option in field["options"]
    )
    save_settings(target, {"runtime": {"webui_font_source": "none"}})
    assert target.config["runtime"]["webui_font_source"] == "none"


class Config(dict):
    saves = 0
    fail_save = False

    def save_config(self):
        self.saves += 1
        if self.fail_save:
            raise OSError("persist failed")


def plugin(config=None):
    result = SimpleNamespace(config=config if config is not None else Config())

    def cfg_get(key, default=None):
        for group in (
            "pixiv_source",
            "content_dedupe",
            "checkin_omnidraw",
            "checkin_basic",
            "checkin_shop",
            "runtime",
        ):
            values = result.config.get(group, {})
            if isinstance(values, dict) and key in values:
                return values[key]
        return result.config.get(key, default)

    result._cfg_get = cfg_get
    return result


def test_settings_snapshot_hides_legacy_fields_and_redacts_secret():
    secret = "private-token-value"
    target = plugin(
        Config(pixiv_refresh_token=secret, runtime={"webui_font_source": "official"})
    )
    result = settings_snapshot(target)
    fields = {
        field["key"]: field for group in result["groups"] for field in group["fields"]
    }
    assert fields["pixiv_refresh_token"]["configured"] is True
    assert fields["pixiv_refresh_token"]["secret"] is True
    assert "value" not in fields["pixiv_refresh_token"]
    assert "default" not in fields["pixiv_refresh_token"]
    assert secret not in repr(result)
    assert "checkin_avatar_enabled" in fields
    assert "group_content_safety_policies" not in fields
    for safety_key in (
        "global_safety_terms",
        "keyword_match_mode",
        "global_blacklisted_illust_ids",
    ):
        assert fields[safety_key]["delegated"] is True
        assert fields[safety_key]["view"] == "safety"
        assert "value" not in fields[safety_key]
    assert fields["safety_groups"]["delegated"] is True
    assert fields["safety_groups"]["view"] == "policies"
    assert "value" not in fields["safety_groups"]
    assert "dedupe_ttl_hours" not in fields
    assert "_grouped_config_migrated" not in fields


def test_every_visible_grouped_schema_item_is_edited_or_delegated():
    schema = json.loads(
        (Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8")
    )
    snapshot = settings_snapshot(plugin())
    actual_groups = {group["key"] for group in snapshot["groups"]}
    expected_groups = {
        key
        for key, group in schema.items()
        if group.get("type") == "object" and isinstance(group.get("items"), dict)
    }
    assert actual_groups == expected_groups
    fields = {
        (group["key"], field["key"]): field
        for group in snapshot["groups"]
        for field in group["fields"]
    }
    for group_key in expected_groups:
        for key, spec in schema[group_key]["items"].items():
            if spec.get("invisible"):
                assert (group_key, key) not in fields
            else:
                field = fields[(group_key, key)]
                delegated = spec.get("type") == "template_list" or key in SAFETY_KEYS
                assert bool(field.get("delegated")) == delegated


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": {"checkin_enabled": False}},
        {"checkin_basic": {"unknown": False}},
        {"runtime": {"_grouped_config_migrated": True}},
        {"content_dedupe": {"group_content_safety_policies": []}},
        {"content_dedupe": {"global_safety_terms": []}},
        {"content_dedupe": {"keyword_match_mode": "loose"}},
        {"content_dedupe": {"global_blacklisted_illust_ids": []}},
        {"content_dedupe": {"safety_groups": []}},
        {"checkin_basic": {"checkin_avatar_enabled": "yes"}},
        {"content_dedupe": {"dedupe_days": 8}},
        {"checkin_basic": {"checkin_ai_greeting_timeout": float("nan")}},
        {"checkin_basic": {"checkin_greeting_mode": "invalid"}},
        {"checkin_basic": {"checkin_hitokoto_categories": ["invalid"]}},
        {"checkin_basic": {"checkin_enabled": 1}},
    ],
)
def test_invalid_patch_changes_nothing(patch):
    target = plugin(
        Config(checkin_basic={"checkin_enabled": True}, unrelated={"keep": 1})
    )
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(target, patch)
    assert target.config == before
    assert target.config.saves == 0


def test_partial_update_preserves_legacy_values_and_calls_save_once():
    target = plugin(
        Config(
            checkin_enabled=False,
            pixiv_source={"lolicon_api_url": "https://old.example/api"},
            content_dedupe={"group_content_safety_policies": [{"group_id": "g1"}]},
            unrelated={"keep": True},
        )
    )
    safety_group = target.config["content_dedupe"]
    save_settings(
        target,
        {
            "checkin_basic": {"checkin_enabled": True},
            "pixiv_source": {"max_count": 8},
        },
    )
    assert target.config["checkin_basic"]["checkin_enabled"] is True
    assert target.config["pixiv_source"]["max_count"] == 8
    assert target.config["content_dedupe"] is safety_group
    assert target.config["checkin_enabled"] is False
    assert target.config["unrelated"] == {"keep": True}
    assert target.config.saves == 1


def test_failed_save_restores_nested_value_and_identity():
    categories = ["全部"]
    checkin = {"checkin_hitokoto_categories": categories, "checkin_enabled": True}
    target = plugin(Config(checkin_basic=checkin))
    target.config.fail_save = True
    with pytest.raises(OSError):
        save_settings(
            target,
            {
                "checkin_basic": {
                    "checkin_enabled": False,
                    "checkin_hitokoto_categories": [],
                }
            },
        )
    assert target.config["checkin_basic"] is checkin
    assert target.config["checkin_basic"]["checkin_hitokoto_categories"] is categories
    assert categories == ["全部"]
    assert target.config["checkin_basic"]["checkin_enabled"] is True


def test_failed_save_restores_original_nonmapping_group():
    target = plugin(Config(checkin_basic=None))
    target.config.fail_save = True
    with pytest.raises(OSError):
        save_settings(target, {"checkin_basic": {"checkin_enabled": False}})
    assert target.config["checkin_basic"] is None


@pytest.mark.asyncio
async def test_settings_api_registers_get_and_update_without_changing_font_contract():
    target = plugin(Config())
    target._cfg_str = lambda key, default="": default
    target.checkin_store = None
    target.image_index = None
    api = PluginWebApi(
        target, plugin_name="x", log_prefix="[x]", internal_error_message="internal"
    )
    app = Quart(__name__)
    app.add_url_rule("/config", view_func=api.config, methods=["GET"])
    app.add_url_rule("/settings", view_func=api.settings, methods=["GET"])
    app.add_url_rule(
        "/settings/update", view_func=api.settings_update, methods=["POST"]
    )
    client = app.test_client()

    font = await client.get("/config")
    assert (await font.get_json())["font_source"] == "mirror"
    snapshot = await client.get("/settings")
    assert (await snapshot.get_json())["success"] is True
    update = await client.post(
        "/settings/update",
        json={"groups": {"checkin_basic": {"checkin_enabled": False}}},
    )
    assert update.status_code == 200
    assert (await update.get_json())["reload_required"] is True
    invalid = await client.post(
        "/settings/update",
        json={"groups": {"runtime": {"_grouped_config_migrated": True}}},
    )
    assert invalid.status_code == 400


def test_slider_bounds_are_inclusive_and_rejected_patches_change_nothing():
    target = plugin(Config(content_dedupe={"dedupe_days": 3}, unrelated={"keep": 1}))
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(
            target,
            {
                "content_dedupe": {"dedupe_days": 8},
                "checkin_basic": {"checkin_enabled": False},
            },
        )
    assert target.config == before and target.config.saves == 0
    save_settings(target, {"content_dedupe": {"dedupe_days": 0}})
    assert target.config["content_dedupe"]["dedupe_days"] == 0
    save_settings(target, {"content_dedupe": {"dedupe_days": 7}})
    assert target.config["content_dedupe"]["dedupe_days"] == 7


def test_step_is_validated_relative_to_min_and_legacy_values_survive():
    target = plugin(Config(pixiv_source={"p_coin_cost": 20}, unrelated={"keep": 1}))
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(target, {"pixiv_source": {"p_coin_cost": 22}})
    assert target.config == before
    assert target.config.saves == 0

    save_settings(target, {"pixiv_source": {"p_coin_cost": 25}})
    save_settings(target, {"pixiv_source": {"p_coin_cost": 20}})
    assert target.config["pixiv_source"]["p_coin_cost"] == 20

    save_settings(target, {"pixiv_source": {"auto_downgrade_original_mb": 10.5}})
    assert target.config["pixiv_source"]["auto_downgrade_original_mb"] == 10.5
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(target, {"pixiv_source": {"auto_downgrade_original_mb": 10.25}})
    assert target.config == before

    # 已存的不合步长旧值不因其它字段保存而被改写或强制修正。
    legacy = plugin(Config(pixiv_source={"p_coin_cost": 22}, unrelated={"keep": 1}))
    assert _field(settings_snapshot(legacy), "p_coin_cost")["value"] == 22
    save_settings(legacy, {"pixiv_source": {"max_count": 6}})
    assert legacy.config["pixiv_source"]["p_coin_cost"] == 22
    assert legacy.config["pixiv_source"]["max_count"] == 6


SCHEMA_PATH = Path(__file__).parents[1] / "_conf_schema.json"


def _field(snapshot, key):
    return next(
        field
        for group in snapshot["groups"]
        for field in group["fields"]
        if field["key"] == key
    )


class _ProviderMeta:
    def __init__(self, provider_id, model):
        self.id = provider_id
        self.model = model


class _Provider:
    def __init__(self, provider_id, model):
        self._meta = _ProviderMeta(provider_id, model)

    def meta(self):
        return self._meta


class _BrokenProvider:
    def meta(self):
        raise RuntimeError("provider meta unavailable")


def _real_config(tmp_path, failing=False):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    config = AstrBotConfig(
        config_path=str(tmp_path / "plugin.json"), schema=deepcopy(schema)
    )
    return config, schema


def test_real_astrbot_config_persists_partial_settings_and_never_exposes_token(
    tmp_path,
):
    config, schema = _real_config(tmp_path)
    target = plugin(config)
    save_settings(
        target,
        {
            "pixiv_source": {"pixiv_refresh_token": "  fresh-token  ", "max_count": 9},
            "checkin_basic": {"checkin_enabled": False},
        },
    )
    assert config["pixiv_source"]["pixiv_refresh_token"] == "fresh-token"
    assert config["pixiv_source"]["max_count"] == 9
    stored = json.loads((tmp_path / "plugin.json").read_text(encoding="utf-8-sig"))
    assert stored["pixiv_source"]["pixiv_refresh_token"] == "fresh-token"
    reloaded = AstrBotConfig(
        config_path=str(tmp_path / "plugin.json"), schema=deepcopy(schema)
    )
    assert reloaded["checkin_basic"]["checkin_enabled"] is False
    assert reloaded["pixiv_source"]["max_count"] == 9

    snapshot = settings_snapshot(target)
    secret = _field(snapshot, "pixiv_refresh_token")
    assert secret["configured"] is True
    assert secret["secret"] is True
    assert "value" not in secret and "default" not in secret
    assert "fresh-token" not in json.dumps(snapshot, ensure_ascii=False)


def test_secret_blank_keeps_value_and_clear_is_a_separate_request_field():
    target = plugin(Config(pixiv_source={"pixiv_refresh_token": "old-token"}))
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(target, {"pixiv_source": {"pixiv_refresh_token": ""}})
    assert target.config == before and target.config.saves == 0

    save_settings(target, {}, ["pixiv_refresh_token"])
    assert target.config["pixiv_source"]["pixiv_refresh_token"] == ""
    save_settings(target, {"pixiv_source": {"pixiv_refresh_token": "  new-token  "}})
    assert target.config["pixiv_source"]["pixiv_refresh_token"] == "new-token"

    for patch, clears in (
        ({"pixiv_source": {"pixiv_refresh_token": "x"}}, ["pixiv_refresh_token"]),
        ({"pixiv_source": {"pixiv_refresh_token": "x"}}, ["unknown"]),
        ({}, "pixiv_refresh_token"),
        ({}, []),
    ):
        with pytest.raises(ValueError):
            save_settings(target, patch, clears)
    assert target.config["pixiv_source"]["pixiv_refresh_token"] == "new-token"


def test_provider_field_uses_public_enumeration_and_keeps_stale_id():
    target = plugin(Config(checkin_basic={"checkin_ai_greeting_provider_id": "stale"}))
    target.context = SimpleNamespace(
        get_all_providers=lambda: [
            _Provider("p1", "model-a"),
            _Provider("p1", "duplicate"),
            _Provider("p2", ""),
        ]
    )
    field = _field(settings_snapshot(target), "checkin_ai_greeting_provider_id")
    assert field["special"] == "select_provider"
    assert field["value"] == "stale"
    assert [item["value"] for item in field["options"]] == ["stale", "p1", "p2"]
    assert field["options"][1]["label"] == "model-a（p1）"
    assert field["options"][2]["label"] == "p2"
    assert "warning" not in field


def test_provider_enumeration_failure_keeps_stored_id_with_warning():
    target = plugin(Config(checkin_basic={"checkin_ai_greeting_provider_id": "kept"}))
    target.context = SimpleNamespace(
        get_all_providers=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    field = _field(settings_snapshot(target), "checkin_ai_greeting_provider_id")
    assert field["value"] == "kept"
    assert [item["value"] for item in field["options"]] == ["kept"]
    assert field["warning"]

    broken = plugin(Config(checkin_basic={"checkin_ai_greeting_provider_id": "kept"}))
    broken.context = SimpleNamespace(get_all_providers=lambda: [_BrokenProvider()])
    field = _field(settings_snapshot(broken), "checkin_ai_greeting_provider_id")
    assert field["options"][0]["value"] == "kept"
    assert field["warning"]

    empty = plugin(Config())
    field = _field(settings_snapshot(empty), "checkin_ai_greeting_provider_id")
    assert field["value"] == "" and field["options"] == []
    assert field["warning"]


def test_multiselect_keeps_stored_legacy_values_without_widening_options():
    target = plugin(
        Config(
            checkin_basic={"checkin_hitokoto_categories": ["全部", "动画", "旧分类"]}
        )
    )
    field = _field(settings_snapshot(target), "checkin_hitokoto_categories")
    # 旧值随已保存配置返回，页面可勾选保留；声明选项不被旧值扩写。
    assert field["value"] == ["全部", "动画", "旧分类"]
    assert "旧分类" not in field["options"]
    # 保留旧值同时取消声明项：旧值仍是当前已存值，允许通过。
    save_settings(
        target,
        {"checkin_basic": {"checkin_hitokoto_categories": ["全部", "旧分类"]}},
    )
    assert target.config["checkin_basic"]["checkin_hitokoto_categories"] == [
        "全部",
        "旧分类",
    ]
    # 旧值通道不放行任意新值。
    with pytest.raises(ValueError):
        save_settings(
            target,
            {
                "checkin_basic": {
                    "checkin_hitokoto_categories": ["全部", "旧分类", "新分类"]
                }
            },
        )
    # 旧值被移除后不再属于当前值，也不能再通过旧值通道加回。
    save_settings(target, {"checkin_basic": {"checkin_hitokoto_categories": ["全部"]}})
    with pytest.raises(ValueError):
        save_settings(
            target,
            {"checkin_basic": {"checkin_hitokoto_categories": ["全部", "旧分类"]}},
        )
    assert target.config["checkin_basic"]["checkin_hitokoto_categories"] == ["全部"]


def test_list_without_options_is_read_only_and_rejected(monkeypatch):
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    schema["runtime"]["items"]["free_list"] = {
        "description": "自由列表",
        "type": "list",
        "default": ["a"],
    }
    monkeypatch.setattr(settings_module, "load_schema", lambda: schema)
    target = plugin(Config(runtime={"free_list": ["a", "b"]}))
    field = _field(settings_snapshot(target), "free_list")
    assert field["editable"] is False
    assert field["value"] == ["a", "b"]
    with pytest.raises(ValueError):
        save_settings(target, {"runtime": {"free_list": []}})


def test_failed_real_save_restores_group_container_and_list_reference(tmp_path):
    config, _schema = _real_config(tmp_path)
    categories = config["checkin_basic"]["checkin_hitokoto_categories"]
    runtime_group = config["runtime"]
    # 真实 AstrBotConfig 序列化失败：不可序列化值只用于触发保存异常。
    config["runtime"]["bad"] = object()
    target = plugin(config)
    with pytest.raises(TypeError):
        save_settings(
            target,
            {
                "checkin_basic": {"checkin_hitokoto_categories": []},
                "runtime": {"rate_limit_seconds": 5},
            },
        )
    assert config["checkin_basic"]["checkin_hitokoto_categories"] is categories
    assert categories == ["全部"]
    assert config["runtime"] is runtime_group
    assert config["runtime"]["rate_limit_seconds"] == 3


@pytest.mark.asyncio
async def test_settings_update_rejects_delegated_hidden_and_blank_secret():
    target = plugin(Config(pixiv_source={"pixiv_refresh_token": "old-token"}))
    target._cfg_str = lambda key, default="": default
    target.checkin_store = None
    target.image_index = None
    api = PluginWebApi(
        target, plugin_name="x", log_prefix="[x]", internal_error_message="internal"
    )
    app = Quart(__name__)
    app.add_url_rule(
        "/settings/update", view_func=api.settings_update, methods=["POST"]
    )
    client = app.test_client()

    for patch in (
        {"content_dedupe": {"global_safety_terms": ["x"]}},
        {"content_dedupe": {"keyword_match_mode": "宽松"}},
        {"content_dedupe": {"global_blacklisted_illust_ids": []}},
        {"content_dedupe": {"safety_groups": []}},
        {"content_dedupe": {"safety_rules_version": 1}},
        {"pixiv_source": {"pixiv_refresh_token": ""}},
    ):
        response = await client.post("/settings/update", json={"groups": patch})
        assert response.status_code == 400
    assert target.config.saves == 0
    assert target.config["pixiv_source"]["pixiv_refresh_token"] == "old-token"

    cleared = await client.post(
        "/settings/update",
        json={"groups": {}, "clear_secrets": ["pixiv_refresh_token"]},
    )
    assert cleared.status_code == 200
    assert (await cleared.get_json())["reload_required"] is True
    assert target.config["pixiv_source"]["pixiv_refresh_token"] == ""


@pytest.mark.asyncio
async def test_settings_update_rejects_step_mismatch_without_mutating():
    target = plugin(Config(pixiv_source={"p_coin_cost": 20}))
    target._cfg_str = lambda key, default="": default
    target.checkin_store = None
    target.image_index = None
    api = PluginWebApi(
        target, plugin_name="x", log_prefix="[x]", internal_error_message="internal"
    )
    app = Quart(__name__)
    app.add_url_rule(
        "/settings/update", view_func=api.settings_update, methods=["POST"]
    )
    client = app.test_client()

    rejected = await client.post(
        "/settings/update", json={"groups": {"pixiv_source": {"p_coin_cost": 22}}}
    )
    assert rejected.status_code == 400
    assert target.config.saves == 0
    assert target.config["pixiv_source"]["p_coin_cost"] == 20

    accepted = await client.post(
        "/settings/update", json={"groups": {"pixiv_source": {"p_coin_cost": 25}}}
    )
    assert accepted.status_code == 200
    assert target.config["pixiv_source"]["p_coin_cost"] == 25
