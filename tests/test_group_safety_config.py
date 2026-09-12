import importlib
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from group_safety import (
    CONFIG_KEY,
    MIGRATION_KEY,
    PRIVATE_CONFIG_KEY,
    GroupSafetyService,
    normalize_policy_entries,
)


def test_group_safety_imports_in_plugin_package_context():
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo.parent))
    try:
        module = importlib.import_module(f"{repo.name}.group_safety")
        assert callable(module.normalize_safety_text)
    finally:
        sys.path.remove(str(repo.parent))


class Config(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0
    def save_config(self):
        self.save_calls += 1
        if self.get("fail"):
            raise RuntimeError("save failed")

class TrackingLegacy:
    def __init__(self, records=None, error=None):
        self.records = records or []
        self.error = error
        self.calls = 0
    async def list_group_content_safety_records(self):
        self.calls += 1
        if self.error:
            raise self.error
        return list(self.records)

class FailOnceConfig(Config):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failures = 1
    def save_config(self):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("save failed")

@pytest.mark.asyncio
async def test_independent_lists_tolerant_warning_and_strict_invalid_matrix(monkeypatch):
    messages=[]
    monkeypatch.setattr("group_safety.logger.warning", messages.append)
    entries, warnings = normalize_policy_entries([{"group_id":"g1","general_only_enabled":True,"builtin_terms_enabled":False,"custom_terms":[" Alpha ","alpha",3],"blacklisted_illust_ids":["001","0","2.5"]}])
    assert entries[0]["custom_terms"] == ["Alpha"] and entries[0]["blacklisted_illust_ids"] == ["1"] and warnings
    service=GroupSafetyService(Config({CONFIG_KEY:[],PRIVATE_CONFIG_KEY:[],MIGRATION_KEY:False}))
    for field,value in [("custom_terms",[3]),("blacklisted_illust_ids",["0"]),("blacklisted_illust_ids",["2.5"])]:
        with pytest.raises(ValueError):
            await service.upsert_group_policy("g1", general_only_enabled=True,builtin_terms_enabled=False,**{field:value})


def test_custom_terms_preserve_punctuation_variants_as_distinct_entries():
    entries, warnings = normalize_policy_entries([{
        "group_id": "g1", "general_only_enabled": True,
        "builtin_terms_enabled": False, "custom_terms": ["r18g", "r-18g"],
    }])
    assert not warnings
    assert entries[0]["custom_terms"] == ["r-18g", "r18g"]

@pytest.mark.asyncio
async def test_apply_policy_field_group_private_all_counts_and_only_field():
    c=Config({CONFIG_KEY:[],PRIVATE_CONFIG_KEY:[],MIGRATION_KEY:True})
    s=GroupSafetyService(c)
    await s.upsert_group_policy("g1",general_only_enabled=True,builtin_terms_enabled=False,custom_terms=["a"],blacklisted_illust_ids=["1"])
    await s.upsert_group_policy("g2",general_only_enabled=False,builtin_terms_enabled=True,custom_terms=[],blacklisted_illust_ids=["2"])
    await s.upsert_private_policy("u1",general_only_enabled=False,builtin_terms_enabled=True,custom_terms=[],blacklisted_illust_ids=["3"])
    result=await s.apply_policy_field("group","g1","custom_terms","all")
    assert result["updated_count"]==2
    assert (await s.get_private_policy("u1"))["custom_terms"]==["a"]

@pytest.mark.asyncio
@pytest.mark.parametrize("field,value,other", [("custom_terms", ["b"], ["1"]), ("blacklisted_illust_ids", ["9"], ["a"])])
@pytest.mark.parametrize("target,expected", [("group", (1, 0)), ("private", (0, 1)), ("all", (1, 1))])
async def test_apply_policy_field_each_scope_preserves_other_fields_and_metadata(field, value, other, target, expected):
    c=Config({CONFIG_KEY:[],PRIVATE_CONFIG_KEY:[],MIGRATION_KEY:True})
    s=GroupSafetyService(c)
    await s.upsert_group_policy("g1",general_only_enabled=False,builtin_terms_enabled=False,custom_terms=(value if field == "custom_terms" else ["old"]),blacklisted_illust_ids=(value if field == "blacklisted_illust_ids" else ["2"]))
    await s.upsert_group_policy("g2",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=["z"],blacklisted_illust_ids=["3"])
    await s.upsert_private_policy("u1",general_only_enabled=False,builtin_terms_enabled=True,custom_terms=["p"],blacklisted_illust_ids=["4"])
    before_calls=c.save_calls
    result=await s.apply_policy_field("group","g1",field,target)
    assert (result["group_updated_count"], result["private_updated_count"]) == expected
    for record in await s.list_policies():
        if record["group_id"] == "g1":
            assert record[field] == value and record["general_only_enabled"] is False and record["builtin_terms_enabled"] is False
            assert record["blacklisted_illust_ids" if field == "custom_terms" else "custom_terms"] == (["2"] if field == "custom_terms" else ["old"])
    assert c.save_calls == before_calls + (1 if sum(expected) else 0)

@pytest.mark.asyncio
async def test_apply_policy_field_zero_change_skips_save_and_rollback_identity():
    c=Config({CONFIG_KEY:[],PRIVATE_CONFIG_KEY:[],MIGRATION_KEY:True})
    s=GroupSafetyService(c)
    await s.upsert_group_policy("g1",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=["a"],blacklisted_illust_ids=[])
    await s.upsert_private_policy("u1",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=[],blacklisted_illust_ids=[])
    group_ref=c[CONFIG_KEY]
    private_ref=c[PRIVATE_CONFIG_KEY]
    before=deepcopy(c[CONFIG_KEY])
    c["fail"]=True
    with pytest.raises(RuntimeError):
        await s.apply_policy_field("group","g1","custom_terms","all")
    assert c[CONFIG_KEY] is group_ref and c[CONFIG_KEY]==before and c[PRIVATE_CONFIG_KEY] is private_ref

@pytest.mark.asyncio
async def test_apply_policy_field_true_zero_change_does_not_save():
    c=Config({CONFIG_KEY:[],PRIVATE_CONFIG_KEY:[],MIGRATION_KEY:True})
    s=GroupSafetyService(c)
    await s.upsert_group_policy("g1",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=["a"],blacklisted_illust_ids=["1"])
    await s.upsert_private_policy("u1",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=["a"],blacklisted_illust_ids=["1"])
    calls = c.save_calls
    result = await s.apply_policy_field("group", "g1", "custom_terms", "all")
    assert result["updated_count"] == 0 and c.save_calls == calls

@pytest.mark.asyncio
@pytest.mark.parametrize("field,source_value", [("custom_terms", ["source"]), ("blacklisted_illust_ids", ["99"])])
@pytest.mark.parametrize("target,expected_ids", [("group", {"g2"}), ("private", {"u1"}), ("all", {"g2", "u1"})])
async def test_apply_policy_field_six_cells_preserve_complete_non_targets(field, source_value, target, expected_ids):
    c=Config({CONFIG_KEY:[],PRIVATE_CONFIG_KEY:[],MIGRATION_KEY:True})
    s=GroupSafetyService(c)
    await s.upsert_group_policy("g1",general_only_enabled=False,builtin_terms_enabled=False,custom_terms=(source_value if field == "custom_terms" else ["g1"]),blacklisted_illust_ids=(source_value if field == "blacklisted_illust_ids" else ["1"]))
    await s.upsert_group_policy("g2",general_only_enabled=True,builtin_terms_enabled=True,custom_terms=["g2"],blacklisted_illust_ids=["2"])
    await s.upsert_private_policy("u1",general_only_enabled=False,builtin_terms_enabled=True,custom_terms=["u1"],blacklisted_illust_ids=["3"])
    before = {"g1": deepcopy(await s.get_group_policy("g1")), "g2": deepcopy(await s.get_group_policy("g2")), "u1": deepcopy(await s.get_private_policy("u1"))}
    calls = c.save_calls
    result = await s.apply_policy_field("group", "g1", field, target)
    assert c.save_calls == calls + 1 and result["updated_count"] == len(expected_ids)
    after = {"g1": await s.get_group_policy("g1"), "g2": await s.get_group_policy("g2"), "u1": await s.get_private_policy("u1")}
    assert after["g1"] == before["g1"]
    for identifier, record in after.items():
        if identifier not in expected_ids:
            assert record == before[identifier]
        else:
            for key in ("general_only_enabled", "builtin_terms_enabled", "group_id", "user_id", "is_default"):
                assert record.get(key) == before[identifier].get(key)
            assert record["custom_terms" if field == "custom_terms" else "blacklisted_illust_ids"] == source_value
            assert record["blacklisted_illust_ids" if field == "custom_terms" else "custom_terms"] == before[identifier]["blacklisted_illust_ids" if field == "custom_terms" else "custom_terms"]

def test_normalize_entries_deduplicates_and_warns():
    entries, warnings = normalize_policy_entries([
        {"group_id": " 100 ", "general_only_enabled": True, "builtin_terms_enabled": True},
        {"group_id": "100", "general_only_enabled": False, "builtin_terms_enabled": False},
    ])
    assert entries[0]["group_id"] == "100"
    assert entries[0]["general_only_enabled"] is True
    assert warnings

@pytest.mark.asyncio
async def test_initialize_invalid_nonempty_config_skips_legacy_and_logs(monkeypatch):
    messages = []
    monkeypatch.setattr("group_safety.logger.warning", messages.append)
    config = Config({CONFIG_KEY: [{"group_id": "", "general_only_enabled": True, "builtin_terms_enabled": True}], MIGRATION_KEY: False})
    legacy = TrackingLegacy([{"group_id": "200", "general_only_enabled": False, "builtin_terms_enabled": True}])
    service = GroupSafetyService(config)
    await service.initialize(legacy)
    assert legacy.calls == 0
    assert any("config" in msg and "entry 0" in msg and "group_id is required" in msg for msg in messages)
    assert await service.list_policies() == []
    assert config[MIGRATION_KEY] is True

@pytest.mark.parametrize("raw", [{"group_id": "200"}, "invalid", None])
@pytest.mark.asyncio
async def test_initialize_nonlist_config_is_config_first(raw, monkeypatch):
    messages = []
    monkeypatch.setattr("group_safety.logger.warning", messages.append)
    config = Config({CONFIG_KEY: raw, MIGRATION_KEY: False})
    legacy = TrackingLegacy([{"group_id": "200", "general_only_enabled": False, "builtin_terms_enabled": True}])
    service = GroupSafetyService(config)
    await service.initialize(legacy)
    assert legacy.calls == 0
    assert any("config" in msg for msg in messages)
    assert (await service.get_policy("200"))["is_default"] is True
    assert config[MIGRATION_KEY] is True

@pytest.mark.asyncio
async def test_initialize_legacy_read_failure_logs_and_keeps_strict(monkeypatch):
    messages = []
    monkeypatch.setattr("group_safety.logger.warning", messages.append)
    config = Config({CONFIG_KEY: [], MIGRATION_KEY: False})
    service = GroupSafetyService(config)
    await service.initialize(TrackingLegacy(error=RuntimeError("broken")))
    assert any("legacy: read failed error_type=RuntimeError" in msg for msg in messages)
    assert config[MIGRATION_KEY] is False
    assert (await service.get_policy("200"))["is_default"] is True
    assert (await service.get_policy("200"))["general_only_enabled"] is True
    assert (await service.get_policy("200"))["builtin_terms_enabled"] is True
    assert await service.list_policies() == []

@pytest.mark.asyncio
async def test_initialize_fail_once_rolls_back_then_retries_incremental(monkeypatch):
    messages = []
    monkeypatch.setattr("group_safety.logger.warning", messages.append)
    config = FailOnceConfig({CONFIG_KEY: [], MIGRATION_KEY: False})
    service = GroupSafetyService(config)
    legacy = TrackingLegacy([
        {"group_id": "200", "general_only_enabled": False, "builtin_terms_enabled": True},
        {"group_id": "100", "general_only_enabled": True, "builtin_terms_enabled": False},
    ])
    await service.initialize(legacy)
    assert legacy.calls == 1
    assert config[CONFIG_KEY] == [] and config[MIGRATION_KEY] is False
    first_policy = await service.get_policy("200")
    assert first_policy["general_only_enabled"] is True
    assert first_policy["builtin_terms_enabled"] is True
    assert first_policy["is_default"] is True
    assert any("legacy: save failed error_type=RuntimeError" in msg for msg in messages)
    await service.initialize(legacy)
    assert legacy.calls == 2
    assert config[MIGRATION_KEY] is True
    policies = await service.list_policies()
    assert [(p["group_id"], p["general_only_enabled"], p["builtin_terms_enabled"], p["is_default"]) for p in policies] == [("100", True, False, False), ("200", False, True, False)]
    assert config[CONFIG_KEY] == [
            {"__template_key": "group_policy", "group_id": "100", "general_only_enabled": True, "builtin_terms_enabled": False, "updated_by": "", "updated_at": "", "custom_terms": [], "blacklisted_illust_ids": []},
            {"__template_key": "group_policy", "group_id": "200", "general_only_enabled": False, "builtin_terms_enabled": True, "updated_by": "", "updated_at": "", "custom_terms": [], "blacklisted_illust_ids": []},
    ]

def test_explicit_strict_is_retained():
    entries, _ = normalize_policy_entries([{"group_id": "1", "general_only_enabled": True, "builtin_terms_enabled": True}])
    assert entries[0]["general_only_enabled"] is True

@pytest.mark.asyncio
async def test_private_policy_namespace_isolated_from_group():
    config = Config({CONFIG_KEY: [], MIGRATION_KEY: False, PRIVATE_CONFIG_KEY: []})
    service = GroupSafetyService(config)
    await service.upsert_private_policy(
        "u1", general_only_enabled=False, builtin_terms_enabled=True
    )
    assert (await service.get_private_policy("u1"))["general_only_enabled"] is False
    assert (await service.get_policy("u1"))["is_default"] is True
    assert config[MIGRATION_KEY] is False
    assert config[CONFIG_KEY] == []


@pytest.mark.asyncio
async def test_private_initialize_survives_group_legacy_read_failure():
    config = Config(
        {
            CONFIG_KEY: [],
            MIGRATION_KEY: False,
            PRIVATE_CONFIG_KEY: [
                {
                    "__template_key": "private_policy",
                    "user_id": "u1",
                    "general_only_enabled": False,
                    "builtin_terms_enabled": True,
                    "updated_at": "2026-09-06T00:00:00+00:00",
                    "updated_by": "web",
                }
            ],
        }
    )
    service = GroupSafetyService(config)
    await service.initialize(TrackingLegacy(error=RuntimeError("broken")))
    policy = await service.get_private_policy("u1")
    assert policy["is_default"] is False
    assert policy["general_only_enabled"] is False
    assert policy["updated_by"] == "web"


@pytest.mark.asyncio
async def test_scope_writes_do_not_touch_other_scope_or_migration_marker():
    config = Config(
        {
            CONFIG_KEY: [{"__template_key": "group_policy", "group_id": "g1", "general_only_enabled": True, "builtin_terms_enabled": False}],
            MIGRATION_KEY: False,
            PRIVATE_CONFIG_KEY: [{"__template_key": "private_policy", "user_id": "u1", "general_only_enabled": False, "builtin_terms_enabled": True}],
        }
    )
    service = GroupSafetyService(config)
    service._install(config[CONFIG_KEY])
    service._install(config[PRIVATE_CONFIG_KEY], private=True)
    group_before = deepcopy(config[CONFIG_KEY])
    private_list = config[PRIVATE_CONFIG_KEY]
    await service.upsert_private_policy("u2", general_only_enabled=False, builtin_terms_enabled=False)
    assert config[CONFIG_KEY] == group_before
    assert config[MIGRATION_KEY] is False
    assert config[PRIVATE_CONFIG_KEY] is private_list
    private_before = deepcopy(config[PRIVATE_CONFIG_KEY])
    await service.upsert_group_policy("g2", general_only_enabled=False, builtin_terms_enabled=True)
    assert config[PRIVATE_CONFIG_KEY] == private_before


@pytest.mark.asyncio
async def test_failed_scope_write_rolls_back_config_and_runtime():
    config = Config({CONFIG_KEY: [], MIGRATION_KEY: True, PRIVATE_CONFIG_KEY: []})
    service = GroupSafetyService(config)
    await service.upsert_private_policy("u1", general_only_enabled=False, builtin_terms_enabled=True)
    before_config = deepcopy(dict(config))
    before_private = await service.list_private_policies()
    config["fail"] = True
    with pytest.raises(RuntimeError, match="save failed"):
        await service.upsert_group_policy("g1", general_only_enabled=False, builtin_terms_enabled=False)
    assert {key: value for key, value in config.items() if key != "fail"} == before_config
    assert await service.list_private_policies() == before_private
    assert await service.list_group_policies() == []


@pytest.mark.asyncio
async def test_group_and_private_metadata_survive_reinitialize():
    config = Config({CONFIG_KEY: [], MIGRATION_KEY: True, PRIVATE_CONFIG_KEY: []})
    service = GroupSafetyService(config)
    await service.upsert_group_policy("g1", general_only_enabled=False, builtin_terms_enabled=True, updated_by="web")
    await service.upsert_private_policy("u1", general_only_enabled=True, builtin_terms_enabled=False, updated_by="web")
    reloaded = GroupSafetyService(config)
    await reloaded.initialize(TrackingLegacy())
    group = await reloaded.get_group_policy("g1")
    private = await reloaded.get_private_policy("u1")
    assert group["updated_by"] == private["updated_by"] == "web"
    assert group["updated_at"] and private["updated_at"]


@pytest.mark.asyncio
async def test_grouped_config_migrates_flat_policies_and_reloads_from_nested_container():
    legacy_entry = {"group_id": "g1", "general_only_enabled": False,
                    "builtin_terms_enabled": True, "custom_terms": ["old"]}
    grouped = {CONFIG_KEY: [], PRIVATE_CONFIG_KEY: [], MIGRATION_KEY: False}
    config = Config({CONFIG_KEY: [legacy_entry], PRIVATE_CONFIG_KEY: [],
                     MIGRATION_KEY: False, "content_dedupe": grouped})
    service = GroupSafetyService(config)
    await service.initialize(TrackingLegacy([]))
    assert grouped[CONFIG_KEY][0]["group_id"] == "g1"
    assert grouped[CONFIG_KEY][0]["custom_terms"] == ["old"]
    assert config[CONFIG_KEY] == [legacy_entry]

    reloaded = GroupSafetyService(config)
    await reloaded.initialize(TrackingLegacy([]))
    assert (await reloaded.get_group_policy("g1"))["custom_terms"] == ["old"]


@pytest.mark.asyncio
async def test_grouped_config_failed_save_restores_nested_identity_and_compatibility():
    grouped = {CONFIG_KEY: [], PRIVATE_CONFIG_KEY: [], MIGRATION_KEY: True}
    config = Config({"content_dedupe": grouped, CONFIG_KEY: [],
                     PRIVATE_CONFIG_KEY: [], MIGRATION_KEY: True})
    service = GroupSafetyService(config)
    await service.upsert_group_policy("g1", general_only_enabled=True,
                                      builtin_terms_enabled=True)
    nested_ref = grouped[CONFIG_KEY]
    before = deepcopy(nested_ref)
    config["fail"] = True
    with pytest.raises(RuntimeError):
        await service.upsert_group_policy("g2", general_only_enabled=False,
                                          builtin_terms_enabled=False)
    assert grouped[CONFIG_KEY] is nested_ref
    assert grouped[CONFIG_KEY] == before
    assert config[CONFIG_KEY] == []
    assert config[MIGRATION_KEY] is True


@pytest.mark.asyncio
async def test_grouped_config_repairs_template_compatibility_after_global_marker():
    group_legacy = {"__template_key": "group_policy", "group_id": "g2",
                    "general_only_enabled": True, "builtin_terms_enabled": True}
    private_legacy = {"__template_key": "private_policy", "user_id": "u2",
                      "general_only_enabled": False, "builtin_terms_enabled": True}
    grouped = {CONFIG_KEY: [], PRIVATE_CONFIG_KEY: [], MIGRATION_KEY: False}
    config = Config({"content_dedupe": grouped, CONFIG_KEY: [group_legacy],
                     PRIVATE_CONFIG_KEY: [private_legacy], MIGRATION_KEY: True,
                     "_grouped_config_migrated": True})
    import importlib
    import sys
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo.parent))
    try:
        GetPxPlugin = importlib.import_module(f"{repo.name}.main").GetPxPlugin
        plugin = GetPxPlugin.__new__(GetPxPlugin)
        plugin.config = config
        plugin._migrate_grouped_config()
    finally:
        sys.path.remove(str(repo.parent))
    assert grouped[CONFIG_KEY] == [group_legacy]
    assert grouped[PRIVATE_CONFIG_KEY] == [private_legacy]
    assert grouped[MIGRATION_KEY] is True


@pytest.mark.parametrize("pre_materialized", [True, False])
def test_grouped_policy_compat_migration_is_atomic_on_save_failure(pre_materialized):
    import importlib
    import sys
    group_entry = {"__template_key": "group_policy", "group_id": "g9",
                   "general_only_enabled": True, "builtin_terms_enabled": True}
    private_entry = {"__template_key": "private_policy", "user_id": "u9",
                     "general_only_enabled": False, "builtin_terms_enabled": True}
    group_ref = [group_entry]
    private_ref = [private_entry]
    config = Config({CONFIG_KEY: group_ref, PRIVATE_CONFIG_KEY: private_ref,
                     MIGRATION_KEY: True, "_grouped_config_migrated": True})
    runtime_ref = {"_grouped_config_migrated": True}
    config["runtime"] = runtime_ref
    content_ref = None
    nested_group_ref = None
    nested_private_ref = None
    if pre_materialized:
        nested_group_ref, nested_private_ref = [], []
        content_ref = {CONFIG_KEY: nested_group_ref, PRIVATE_CONFIG_KEY: nested_private_ref,
                       MIGRATION_KEY: False}
        config["content_dedupe"] = content_ref
    runtime_before = deepcopy(runtime_ref)
    config["fail"] = True
    repo = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo.parent))
    try:
        GetPxPlugin = importlib.import_module(f"{repo.name}.main").GetPxPlugin
        plugin = GetPxPlugin.__new__(GetPxPlugin)
        plugin.config = config
        plugin._migrate_grouped_config()
    finally:
        sys.path.remove(str(repo.parent))
    assert config[CONFIG_KEY] is group_ref and config[PRIVATE_CONFIG_KEY] is private_ref
    assert config[CONFIG_KEY] == [group_entry]
    assert config[PRIVATE_CONFIG_KEY] == [private_entry]
    assert config[MIGRATION_KEY] is True
    assert config["runtime"] is runtime_ref and config["runtime"] == runtime_before
    if pre_materialized:
        assert config["content_dedupe"] is content_ref
        assert config["content_dedupe"][CONFIG_KEY] is nested_group_ref
        assert config["content_dedupe"][PRIVATE_CONFIG_KEY] is nested_private_ref
        assert config["content_dedupe"][CONFIG_KEY] == []
        assert config["content_dedupe"][PRIVATE_CONFIG_KEY] == []
        assert config["content_dedupe"][MIGRATION_KEY] is False
    else:
        assert "content_dedupe" not in config



def test_lolicon_exclude_ai_document_contracts_are_consistent():
    import json
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    hint = schema["pixiv_source"]["items"]["lolicon_exclude_ai"]["hint"]
    readme_line = next(line for line in Path("README.md").read_text(encoding="utf-8").splitlines()
                       if "`lolicon_exclude_ai`" in line)
    config_line = next(line for line in Path("docs/project/configuration.md").read_text(encoding="utf-8").splitlines()
                       if "`lolicon_exclude_ai`" in line)
    for segment in (hint, readme_line, config_line):
        assert "AI" in segment and "当前会话" in segment and "强制普通分级" in segment
        assert "R18 始终关闭" not in segment
    assert "lolicon_exclude_ai" in schema["pixiv_source"]["items"]


@pytest.mark.asyncio
async def test_nested_apply_all_failure_rolls_back_lists_markers_and_runtime():
    group_nested = [{"__template_key": "group_policy", "group_id": "g1",
                     "general_only_enabled": True, "builtin_terms_enabled": True,
                     "custom_terms": ["old"], "blacklisted_illust_ids": ["1"]}]
    private_nested = [{"__template_key": "private_policy", "user_id": "u1",
                       "general_only_enabled": True, "builtin_terms_enabled": True,
                       "custom_terms": [], "blacklisted_illust_ids": []}]
    group_compat = [{"group_id": "legacy"}]
    private_compat = [{"user_id": "legacy"}]
    config = Config({"content_dedupe": {CONFIG_KEY: group_nested,
        PRIVATE_CONFIG_KEY: private_nested, MIGRATION_KEY: True},
        CONFIG_KEY: group_compat, PRIVATE_CONFIG_KEY: private_compat,
        MIGRATION_KEY: True, "runtime": {"_grouped_config_migrated": True}})
    service = GroupSafetyService(config)
    await service.initialize(TrackingLegacy([]))
    config.save_calls = 0
    group_ref, private_ref = group_nested, private_nested
    group_before, private_before = deepcopy(group_nested), deepcopy(private_nested)
    runtime_ref = config["runtime"]
    runtime_before = deepcopy(runtime_ref)
    service_groups_before = await service.list_policies()
    service_private_before = await service.list_private_policies()
    top_marker_present = MIGRATION_KEY in config
    top_marker_before = config.get(MIGRATION_KEY)
    compat_refs = (config[CONFIG_KEY], config[PRIVATE_CONFIG_KEY])
    compat_before = (deepcopy(compat_refs[0]), deepcopy(compat_refs[1]))
    config["fail"] = True
    with pytest.raises(RuntimeError):
        await service.apply_policy_field("group", "g1", "custom_terms", "all")
    assert config.save_calls == 1
    assert config["content_dedupe"][CONFIG_KEY] is group_ref
    assert config["content_dedupe"][PRIVATE_CONFIG_KEY] is private_ref
    assert group_nested == group_before and private_nested == private_before
    assert config[CONFIG_KEY] is compat_refs[0] and config[PRIVATE_CONFIG_KEY] is compat_refs[1]
    assert config[CONFIG_KEY] == compat_before[0] and config[PRIVATE_CONFIG_KEY] == compat_before[1]
    assert config["content_dedupe"][MIGRATION_KEY] is True
    assert config["runtime"] is runtime_ref and config["runtime"] == runtime_before
    assert await service.list_policies() == service_groups_before
    assert await service.list_private_policies() == service_private_before
    assert (MIGRATION_KEY in config) is top_marker_present
    assert config.get(MIGRATION_KEY) == top_marker_before
