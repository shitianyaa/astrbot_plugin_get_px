from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from quart import Quart

from plugin_api.api import PluginWebApi
from plugin_api.settings import save_settings, settings_snapshot


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
        for group in ("pixiv_source", "content_dedupe", "checkin_omnidraw", "checkin_basic", "checkin_shop", "runtime"):
            values = result.config.get(group, {})
            if isinstance(values, dict) and key in values:
                return values[key]
        return result.config.get(key, default)

    result._cfg_get = cfg_get
    return result


def test_settings_snapshot_hides_legacy_fields_and_redacts_secret():
    secret = "private-token-value"
    target = plugin(Config(pixiv_refresh_token=secret, runtime={"webui_font_source": "official"}))
    result = settings_snapshot(target)
    fields = {field["key"]: field for group in result["groups"] for field in group["fields"]}
    assert fields["pixiv_refresh_token"]["configured"] is True
    assert fields["pixiv_refresh_token"]["secret"] is True
    assert "value" not in fields["pixiv_refresh_token"]
    assert "default" not in fields["pixiv_refresh_token"]
    assert secret not in repr(result)
    assert "checkin_affection_bonus_step_percent" in fields
    assert fields["group_content_safety_policies"]["delegated"] is True
    assert fields["group_content_safety_policies"]["view"] == "policies"
    assert "dedupe_ttl_hours" not in fields
    assert "_grouped_config_migrated" not in fields


def test_every_visible_grouped_schema_item_is_edited_or_delegated():
    schema = json.loads((Path(__file__).parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    snapshot = settings_snapshot(plugin())
    actual_groups = {group["key"] for group in snapshot["groups"]}
    expected_groups = {
        key for key, group in schema.items()
        if group.get("type") == "object" and isinstance(group.get("items"), dict)
    }
    assert actual_groups == expected_groups
    fields = {
        (group["key"], field["key"]): field
        for group in snapshot["groups"] for field in group["fields"]
    }
    for group_key in expected_groups:
        for key, spec in schema[group_key]["items"].items():
            if spec.get("invisible"):
                assert (group_key, key) not in fields
            else:
                field = fields[(group_key, key)]
                assert bool(field.get("delegated")) == (spec.get("type") == "template_list")


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": {"checkin_enabled": False}},
        {"checkin_basic": {"unknown": False}},
        {"runtime": {"_grouped_config_migrated": True}},
        {"content_dedupe": {"group_content_safety_policies": []}},
        {"checkin_basic": {"checkin_affection_bonus_step_percent": True}},
        {"checkin_basic": {"checkin_affection_bonus_step_percent": 26}},
        {"checkin_basic": {"checkin_ai_greeting_timeout": float("nan")}},
        {"checkin_basic": {"checkin_greeting_mode": "invalid"}},
        {"checkin_basic": {"checkin_hitokoto_categories": ["invalid"]}},
        {"checkin_basic": {"checkin_enabled": 1}},
    ],
)
def test_invalid_patch_changes_nothing(patch):
    target = plugin(Config(checkin_basic={"checkin_enabled": True}, unrelated={"keep": 1}))
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(target, patch)
    assert target.config == before
    assert target.config.saves == 0


def test_partial_update_preserves_legacy_values_and_calls_save_once():
    target = plugin(Config(
        checkin_enabled=False,
        pixiv_source={"lolicon_api_url": "https://old.example/api"},
        content_dedupe={"group_content_safety_policies": [{"group_id": "g1"}]},
        unrelated={"keep": True},
    ))
    safety_group = target.config["content_dedupe"]
    save_settings(target, {
        "checkin_basic": {"checkin_enabled": True},
        "pixiv_source": {"max_count": 8},
    })
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
        save_settings(target, {"checkin_basic": {"checkin_enabled": False, "checkin_hitokoto_categories": []}})
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
    api = PluginWebApi(target, plugin_name="x", log_prefix="[x]", internal_error_message="internal")
    app = Quart(__name__)
    app.add_url_rule("/config", view_func=api.config, methods=["GET"])
    app.add_url_rule("/settings", view_func=api.settings, methods=["GET"])
    app.add_url_rule("/settings/update", view_func=api.settings_update, methods=["POST"])
    client = app.test_client()

    font = await client.get("/config")
    assert (await font.get_json())["font_source"] == "mirror"
    snapshot = await client.get("/settings")
    assert (await snapshot.get_json())["success"] is True
    update = await client.post("/settings/update", json={"groups": {"checkin_basic": {"checkin_enabled": False}}})
    assert update.status_code == 200
    assert (await update.get_json())["reload_required"] is True
    invalid = await client.post("/settings/update", json={"groups": {"runtime": {"_grouped_config_migrated": True}}})
    assert invalid.status_code == 400

def test_coin_bounds_validate_merged_partial_patch_atomically():
    target = plugin(Config(checkin_basic={"checkin_coin_min": 50, "checkin_coin_max": 100}))
    before = deepcopy(target.config)
    with pytest.raises(ValueError):
        save_settings(target, {"checkin_basic": {"checkin_coin_min": 101, "checkin_enabled": False}})
    assert target.config == before and target.config.saves == 0
    save_settings(target, {"checkin_basic": {"checkin_coin_min": 100, "checkin_coin_max": 100}})
    assert target.config["checkin_basic"]["checkin_coin_min"] == 100
    save_settings(target, {"checkin_basic": {"checkin_coin_min": 0, "checkin_coin_max": 0}})
    assert target.config["checkin_basic"]["checkin_coin_max"] == 0


def test_coin_bounds_use_flat_fallback_and_bad_existing_pair_only_blocks_bound_edits():
    flat = plugin(Config(checkin_coin_min=20, checkin_coin_max=40))
    save_settings(flat, {"checkin_basic": {"checkin_coin_min": 30}})
    assert flat.config["checkin_basic"]["checkin_coin_min"] == 30
    bad = plugin(Config(checkin_basic={"checkin_coin_min": 200, "checkin_coin_max": 100}))
    save_settings(bad, {"checkin_basic": {"checkin_enabled": False}})
    with pytest.raises(ValueError):
        save_settings(bad, {"checkin_basic": {"checkin_coin_max": 150}})
    save_settings(bad, {"checkin_basic": {"checkin_coin_min": 80, "checkin_coin_max": 80}})
    assert bad.config["checkin_basic"]["checkin_coin_min"] == 80
