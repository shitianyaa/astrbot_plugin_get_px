"""Schema-driven settings snapshots and transactional partial updates."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"
SECRET_KEYS = frozenset({"pixiv_refresh_token"})


def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as source:
        return json.load(source)


def _visible_groups(schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: group
        for name, group in schema.items()
        if group.get("type") == "object"
        and not group.get("invisible")
        and isinstance(group.get("items"), dict)
    }


def _effective(plugin: Any, key: str, default: Any) -> Any:
    getter = getattr(plugin, "_cfg_get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    config = getattr(plugin, "config", {})
    for group in _visible_groups(load_schema()):
        nested = config.get(group, {})
        if isinstance(nested, dict) and key in nested:
            return nested[key]
    return config.get(key, default)


def settings_snapshot(plugin: Any) -> dict[str, Any]:
    schema = load_schema()
    groups = []
    for group_key, group in _visible_groups(schema).items():
        fields = []
        for key, spec in group["items"].items():
            if spec.get("invisible"):
                continue
            descriptor = {
                "key": key,
                "label": spec.get("description", key),
                "type": spec.get("type", "string"),
                "hint": spec.get("hint", ""),
                "options": deepcopy(spec.get("options", [])),
                "slider": deepcopy(spec.get("slider")),
            }
            if key in SECRET_KEYS:
                descriptor["secret"] = True
                descriptor["configured"] = bool(_effective(plugin, key, ""))
            elif spec.get("type") == "template_list":
                descriptor["delegated"] = True
                descriptor["view"] = "policies"
            else:
                descriptor["default"] = deepcopy(spec.get("default"))
                descriptor["value"] = deepcopy(
                    _effective(plugin, key, descriptor["default"])
                )
            fields.append(descriptor)
        groups.append(
            {
                "key": group_key,
                "label": group.get("description", group_key),
                "hint": group.get("hint", ""),
                "fields": fields,
            }
        )
    return {"success": True, "groups": groups}


def _validate_value(key: str, value: Any, spec: dict[str, Any]) -> Any:
    kind = spec.get("type")
    if key in SECRET_KEYS:
        if not isinstance(value, str):
            raise ValueError(f"{spec.get('description', key)}必须为文本")
        return value
    if kind == "bool":
        valid = isinstance(value, bool)
    elif kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "float":
        valid = isinstance(value, int) and not isinstance(value, bool)
        if isinstance(value, float):
            valid = math.isfinite(value)
    elif kind in {"string", "text"}:
        valid = isinstance(value, str)
    elif kind == "list":
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    else:
        raise ValueError(f"{spec.get('description', key)}不能通过此设置页编辑")
    if not valid:
        raise ValueError(f"{spec.get('description', key)}类型不正确")
    if kind == "list" and spec.get("options"):
        invalid = [item for item in value if item not in spec["options"]]
        if invalid:
            raise ValueError(f"{spec.get('description', key)}包含无效选项")
    if "options" in spec and kind != "list" and value not in spec["options"]:
        raise ValueError(f"{spec.get('description', key)}不在可选范围内")
    slider = spec.get("slider")
    if slider and kind in {"int", "float"}:
        if value < slider["min"] or value > slider["max"]:
            raise ValueError(
                f"{spec.get('description', key)}需在 {slider['min']} 至 {slider['max']} 之间"
            )
    return value


def save_settings(plugin: Any, patch: Any) -> None:
    if not isinstance(patch, dict) or not patch:
        raise ValueError("设置内容格式不正确")
    schema = load_schema()
    groups = _visible_groups(schema)
    validated: dict[str, dict[str, Any]] = {}
    for group_key, values in patch.items():
        if group_key not in groups or not isinstance(values, dict) or not values:
            raise ValueError("设置分组不正确")
        specs = groups[group_key]["items"]
        for key, value in values.items():
            spec = specs.get(key)
            if not spec or spec.get("invisible") or spec.get("type") == "template_list":
                raise ValueError("包含不能在此页面编辑的设置项")
            validated.setdefault(group_key, {})[key] = _validate_value(key, value, spec)

    coin_patch = validated.get("checkin_basic", {})
    if {"checkin_coin_min", "checkin_coin_max"} & coin_patch.keys():
        specs = groups["checkin_basic"]["items"]
        current: dict[str, Any] = {}
        for key in ("checkin_coin_min", "checkin_coin_max"):
            spec = specs[key]
            current[key] = _validate_value(
                key,
                coin_patch.get(key, _effective(plugin, key, spec.get("default"))),
                spec,
            )
        if current["checkin_coin_min"] > current["checkin_coin_max"]:
            raise ValueError("签到基础金币下限不能大于上限；请同时修正两个范围值")

    config = getattr(plugin, "config", None)
    if config is None or not hasattr(config, "save_config"):
        raise RuntimeError("配置保存服务不可用")

    # Keep the original nested container objects; partial edits must not disturb
    # policy service references or other unrelated grouped settings.
    originals: dict[
        str, tuple[Any, bool, bool, Any, dict[str, tuple[bool, Any, Any]]]
    ] = {}
    try:
        for group_key, values in validated.items():
            group = config.get(group_key)
            had_group = isinstance(group, dict)
            root_had_group = group_key in config
            root_group_value = group
            if not had_group:
                group = {}
                config[group_key] = group
            originals[group_key] = (
                group,
                had_group,
                root_had_group,
                root_group_value,
                {
                    key: (key in group, group.get(key), deepcopy(group.get(key)))
                    for key in values
                },
            )
            group.update(values)
        config.save_config()
    except Exception:
        for group_key, original in originals.items():
            group, had_group, root_had_group, root_group_value, old_values = original
            for key, (present, original_value, old_value) in old_values.items():
                if present:
                    if isinstance(original_value, list) and isinstance(old_value, list):
                        original_value[:] = old_value
                    elif isinstance(original_value, dict) and isinstance(old_value, dict):
                        original_value.clear()
                        original_value.update(old_value)
                    group[key] = original_value
                else:
                    group.pop(key, None)
            if root_had_group:
                config[group_key] = group if had_group else root_group_value
            else:
                config.pop(group_key, None)
            if had_group:
                config[group_key] = group
        raise
