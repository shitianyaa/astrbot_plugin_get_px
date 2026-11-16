from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.api import AstrBotConfig

from group_safety import (
    CONFIG_KEY,
    IDS_KEY,
    LEGACY_KEYS,
    MODE_KEY,
    TERMS_KEY,
    VERSION_KEY,
    SessionSafetyService,
)


class Config(dict):
    def save_config(self):
        if getattr(self, "fail", False):
            raise OSError("save failed")


def legacy_sources():
    return (
        SimpleNamespace(list_group_content_safety_records=AsyncMock(return_value=[])),
        SimpleNamespace(
            list_safety_terms=AsyncMock(return_value=[]),
            list_blacklist_illusts=AsyncMock(return_value=[]),
        ),
    )


@pytest.mark.asyncio
async def test_named_groups_merge_global_rules_and_isolate_full_sessions():
    config = Config(content_dedupe={})
    service = SessionSafetyService(config)
    await service.update_global(
        {TERMS_KEY: ["Ａlpha"], IDS_KEY: ["001"], MODE_KEY: "严格"}
    )
    group = await service.save_group(
        {
            "name": "插画",
            "sessions": ["bot-a:GroupMessage:1", "bot-a:FriendMessage:2"],
            "allow_r18": True,
            "custom_terms": ["Beta"],
        }
    )
    policy = await service.resolve("bot-a:GroupMessage:1")
    assert not policy.general_only_enabled
    assert policy.match_mode == "严格"
    from pixiv.safety import normalize_safety_text

    assert {normalize_safety_text(term) for term in policy.custom_terms} == {
        "alpha",
        "beta",
    }
    assert policy.blacklisted_illust_ids == ("1",)
    assert (await service.resolve("bot-b:GroupMessage:1")).general_only_enabled
    assert (await service.resolve("bot-a:GroupMessage:2")).general_only_enabled
    assert (
        await service.resolve("bot-a:FriendMessage:2")
    ).custom_terms == policy.custom_terms
    frozen_identity = policy.cache_identity()
    await service.update_global({MODE_KEY: "宽松"})
    assert policy.cache_identity() == frozen_identity
    assert (
        await service.resolve("bot-a:GroupMessage:1")
    ).cache_identity() != frozen_identity
    reopened = SessionSafetyService(config)
    assert await reopened.settings() == await service.settings()
    await service.remove_group(group["group_id"])
    default = await service.resolve("bot-a:GroupMessage:1")
    assert default.general_only_enabled and len(default.custom_terms) == 1
    assert default.blacklisted_illust_ids == ("1",)


@pytest.mark.asyncio
async def test_duplicate_session_invalid_inputs_and_save_failure_preserve_config():
    config = Config(content_dedupe={})
    service = SessionSafetyService(config)
    first = await service.save_group(
        {"name": "first", "sessions": ["bot:GroupMessage:1"]}
    )
    ref = config["content_dedupe"][CONFIG_KEY]
    before = deepcopy(config)
    for payload in (
        {"name": "second", "sessions": ["bot:GroupMessage:1"]},
        {"name": "invalid", "sessions": ["1"]},
        {"name": "invalid", "allow_r18": "true"},
        {"name": "invalid", "custom_terms": [None]},
    ):
        with pytest.raises(ValueError):
            await service.save_group(payload)
    config.fail = True
    with pytest.raises(OSError):
        await service.save_group({"group_id": first["group_id"], "allow_r18": True})
    assert config == before and config["content_dedupe"][CONFIG_KEY] is ref
    assert (await service.resolve("bot:GroupMessage:1")).general_only_enabled


@pytest.mark.asyncio
async def test_framework_load_archives_legacy_before_reset_and_preserves_global(
    tmp_path,
):
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(
            encoding="utf-8"
        )
    )
    path = tmp_path / "config.json"
    old = {
        LEGACY_KEYS[0]: [
            {
                "group_id": "1",
                "general_only_enabled": False,
                "custom_terms": ["old-local"],
            }
        ],
        LEGACY_KEYS[1]: [],
        LEGACY_KEYS[2]: True,
    }
    path.write_text(json.dumps({"content_dedupe": old}), encoding="utf-8")
    config = AstrBotConfig(config_path=str(path), schema=schema)
    service = SessionSafetyService(config)
    legacy, index = legacy_sources()
    import sqlite3
    from pixiv.index import ImageIndexStore

    real_index = ImageIndexStore(tmp_path)
    with sqlite3.connect(real_index._db_path) as connection:
        connection.execute(
            "CREATE TABLE content_safety_terms (term TEXT, normalized_term TEXT, added_by TEXT, added_at TEXT)"
        )
        connection.execute(
            "INSERT INTO content_safety_terms VALUES ('old-global', 'oldglobal', 'legacy', 'now')"
        )
    await real_index.add_blacklist_illust(illust_id="10")
    try:
        await service.initialize(legacy, real_index, tmp_path)
    finally:
        real_index.close()
    archive = (await service.legacy_archives())[0]
    assert archive["legacy"]["config"][LEGACY_KEYS[0]] == old[LEGACY_KEYS[0]]
    assert config["content_dedupe"][LEGACY_KEYS[0]] == []
    assert config["content_dedupe"][VERSION_KEY] == 1
    assert (await service.settings())[TERMS_KEY] == ["old-global"]
    assert (await service.settings())[IDS_KEY] == ["10"]
    assert (await service.resolve("bot:GroupMessage:1")).general_only_enabled
    # 不重新读取旧库，防止用户删除全局词后重载又恢复旧词。
    await service.change_global_item(TERMS_KEY, "old-global", remove=True)
    await service.initialize(legacy, index, tmp_path)
    assert len(await service.legacy_archives()) == 1
    assert (await service.settings())[TERMS_KEY] == []
    index.list_safety_terms.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_or_config_failure_never_clears_legacy(tmp_path, monkeypatch):
    old = [{"group_id": "1", "general_only_enabled": False}]
    config = Config(content_dedupe={LEGACY_KEYS[0]: old})
    before = deepcopy(config)
    service = SessionSafetyService(config)
    legacy, index = legacy_sources()
    original = service._write_archive
    monkeypatch.setattr(
        service,
        "_write_archive",
        lambda _: (_ for _ in ()).throw(OSError("archive failed")),
    )
    with pytest.raises(OSError):
        await service.initialize(legacy, index, tmp_path)
    assert config == before
    monkeypatch.setattr(service, "_write_archive", original)
    config.fail = True
    with pytest.raises(OSError):
        await service.initialize(legacy, index, tmp_path)
    assert config == before
    assert (await service.legacy_archives())[0]["legacy"]["config"][
        LEGACY_KEYS[0]
    ] == old
    config.fail = False
    archive_path = next((tmp_path / "safety_legacy_backups").glob("legacy-*.json"))
    archive_data = archive_path.read_bytes()
    archive_path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="备份校验失败"):
        await service.initialize(legacy, index, tmp_path)
    assert config == before
    archive_path.write_bytes(archive_data)
    await service.initialize(legacy, index, tmp_path)
    assert len(await service.legacy_archives()) == 1


@pytest.mark.asyncio
async def test_global_list_validation_and_concurrent_updates():
    import asyncio

    service = SessionSafetyService(Config(content_dedupe={}))
    with pytest.raises(ValueError):
        await SessionSafetyService(Config(content_dedupe=None)).settings()
    for value in ([0], ["0"], ["-1"], [None]):
        with pytest.raises(ValueError):
            await service.update_global({IDS_KEY: value})
    await asyncio.gather(
        *(
            service.change_global_item(TERMS_KEY, term)
            for term in ("alpha", "beta", "ＡLPHA")
        )
    )
    assert (await service.settings())[TERMS_KEY] == ["alpha", "beta"]
