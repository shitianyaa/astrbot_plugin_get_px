"""3.8.0 已迁移配置经过真实 AstrBot 加载后保留分组值与策略。"""

from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api import AstrBotConfig

from group_safety import (
    CONFIG_KEY,
    MIGRATION_KEY,
    PRIVATE_CONFIG_KEY,
    SessionSafetyService,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
try:
    GetPxPlugin = importlib.import_module(f"{ROOT.name}.main").GetPxPlugin
finally:
    sys.path.remove(str(ROOT.parent))

SCHEMA = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
FIXTURE = ROOT / "tests/fixtures/config_v3_8_0_grouped.json"
RETIRED = {"dedupe_ttl_hours", "dedupe_days_migrated", "_grouped_config_migrated", "lolicon_api_url"}


def test_schema_contains_only_formal_groups_and_required_policy_marker():
    assert tuple(SCHEMA) == GetPxPlugin._CONFIG_GROUP_KEYS
    for group in SCHEMA.values():
        assert group["type"] == "object"
        assert not RETIRED.intersection(group["items"])
    assert MIGRATION_KEY in SCHEMA["content_dedupe"]["items"]


@pytest.mark.asyncio
async def test_v3_8_0_grouped_config_survives_framework_load_save_and_reload(tmp_path):
    path = tmp_path / "plugin.json"
    path.write_bytes(FIXTURE.read_bytes())
    old = json.loads(FIXTURE.read_text(encoding="utf-8"))
    config = AstrBotConfig(config_path=str(path), schema=deepcopy(SCHEMA))
    expected = {
        group: {key: old[group].get(key, field["default"]) for key, field in spec["items"].items()}
        for group, spec in SCHEMA.items()
    }
    assert dict(config) == expected
    assert json.loads(path.read_text(encoding="utf-8-sig")) == expected

    plugin = object.__new__(GetPxPlugin)
    plugin.config = config
    plugin._cleanup_retired_transport_config()
    for group in expected.values():
        for key, value in group.items():
            assert plugin._cfg_get(key) == value
    assert plugin._cfg_int("dedupe_days", 1, 0, 7) == 7
    assert plugin._cfg_int("p_coin_cost", 20, 0, 200) == 35
    assert plugin._cfg_bool("checkin_enabled", True) is False

    legacy = SimpleNamespace(list_group_content_safety_records=AsyncMock())
    service = SessionSafetyService(config)
    assert await service.initialize(legacy) is True
    legacy.list_group_content_safety_records.assert_not_awaited()
    group_before = deepcopy(await service.get_group_policy("10001"))
    private_before = deepcopy(await service.get_private_policy("20001"))
    assert group_before["custom_terms"] == ["fixture-group"]
    assert group_before["blacklisted_illust_ids"] == ["11"]
    assert private_before["custom_terms"] == ["fixture-private"]
    assert private_before["blacklisted_illust_ids"] == ["22"]
    assert group_before["updated_by"] == private_before["updated_by"] == "web"

    await service.upsert_group_policy(
        "10002", general_only_enabled=True, builtin_terms_enabled=True
    )
    config["pixiv_source"]["preferred_image_source"] = "Pixiv"
    config.save_config()
    reloaded = AstrBotConfig(config_path=str(path), schema=deepcopy(SCHEMA))
    assert reloaded["pixiv_source"]["preferred_image_source"] == "Pixiv"
    reopened = SessionSafetyService(reloaded)
    await reopened.initialize(legacy)
    assert await reopened.get_group_policy("10001") == group_before
    assert await reopened.get_private_policy("20001") == private_before
    assert (await reopened.get_group_policy("10002"))["is_default"] is False
    assert reloaded["content_dedupe"][MIGRATION_KEY] is True
    assert (
        reloaded["content_dedupe"][PRIVATE_CONFIG_KEY]
        == expected["content_dedupe"][PRIVATE_CONFIG_KEY]
    )
    assert set(reloaded) == set(SCHEMA)


@pytest.mark.asyncio
async def test_empty_grouped_lists_do_not_resurrect_stale_policies(tmp_path):
    old = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for key in (CONFIG_KEY, PRIVATE_CONFIG_KEY):
        old[key] = old["content_dedupe"][key]
        old["content_dedupe"][key] = []
    path = tmp_path / "plugin.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    config = AstrBotConfig(config_path=str(path), schema=deepcopy(SCHEMA))
    legacy = SimpleNamespace(list_group_content_safety_records=AsyncMock())
    service = SessionSafetyService(config)
    await service.initialize(legacy)
    assert await service.list_group_policies() == []
    assert await service.list_private_policies() == []
    legacy.list_group_content_safety_records.assert_not_awaited()
    assert CONFIG_KEY not in config and PRIVATE_CONFIG_KEY not in config


def test_new_install_has_grouped_defaults_without_retired_keys(tmp_path):
    config = AstrBotConfig(
        config_path=str(tmp_path / "new.json"), schema=deepcopy(SCHEMA)
    )
    assert tuple(config) == GetPxPlugin._CONFIG_GROUP_KEYS
    assert config["content_dedupe"]["dedupe_days"] == 1
    assert config["content_dedupe"][CONFIG_KEY] == []
    assert config["content_dedupe"][PRIVATE_CONFIG_KEY] == []
    assert config["runtime"]["request_timeout"] == 30.0
    assert config["pixiv_source"]["preferred_image_source"] == "Lolicon"
    assert SCHEMA["pixiv_source"]["items"]["preferred_image_source"]["options"] == ["Lolicon", "Pixiv"]


def test_init_client_ignores_retired_url_and_reuses_pixiv_client(monkeypatch):
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"pixiv_source": {
        "lolicon_api_url": "https://example.invalid/setu/v2",
        "pixiv_refresh_token": "fixture-token",
    }}
    pixiv = SimpleNamespace()
    factory = Mock(return_value=pixiv)
    monkeypatch.setattr(sys.modules[GetPxPlugin.__module__], "PixivClient", factory)
    plugin._init_client()
    lolicon = plugin.lolicon_client
    plugin._init_client()
    assert plugin.lolicon_client is lolicon and lolicon.available
    assert plugin.client is pixiv
    factory.assert_called_once_with(refresh_token="fixture-token", request_timeout=30.0)
