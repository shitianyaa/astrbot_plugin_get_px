"""Schema-driven settings snapshots and transactional partial updates."""

from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any, Sequence

from astrbot.api import logger


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "_conf_schema.json"
LOG_PREFIX = "[GetPx]"
SECRET_KEYS = frozenset({"pixiv_refresh_token"})
PROVIDER_SPECIAL = "select_provider"
# 全局安全规则与会话安全分组由 SessionSafetyService 独占读写；
# 通用设置页只展示跳转入口，禁止通过通用接口写入，避免覆盖安全列表引用。
SAFETY_KEYS = frozenset(
    {
        "global_safety_terms",
        "keyword_match_mode",
        "global_blacklisted_illust_ids",
        "safety_groups",
    }
)
# 全局规则在「内容安全」页维护，会话分组与模板列表在「安全分组」页维护；
# 设置页的跳转入口必须落到真正能编辑该项的页面。
DELEGATED_VIEWS = {
    "global_safety_terms": "safety",
    "keyword_match_mode": "safety",
    "global_blacklisted_illust_ids": "safety",
    "safety_groups": "policies",
}


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


def _secret_groups(schema: dict[str, Any]) -> dict[str, str]:
    """令牌键所在分组，用于把显式清除写入同一嵌套容器。"""
    return {
        key: group_key
        for group_key, group in _visible_groups(schema).items()
        for key in group["items"]
        if key in SECRET_KEYS
    }


def _provider_choices(plugin: Any) -> tuple[list[dict[str, str]], str]:
    """用公开的 context.get_all_providers() 枚举文本模型；失败时给出安全提示。"""
    context = getattr(plugin, "context", None)
    lister = getattr(context, "get_all_providers", None)
    if not callable(lister):
        return [], "模型列表不可用；当前已保存的模型 ID 保持不变。"
    try:
        providers = lister() or ()
    except Exception:
        return [], "模型列表读取失败；当前已保存的模型 ID 保持不变。"
    choices: list[dict[str, str]] = []
    seen: set[str] = set()
    failed = 0
    for provider in providers:
        try:
            meta = provider.meta()
            provider_id = str(meta.id or "").strip()
            model = str(meta.model or "").strip()
        except Exception:
            failed += 1
            continue
        if not provider_id or provider_id in seen:
            continue
        seen.add(provider_id)
        choices.append(
            {
                "value": provider_id,
                "label": f"{model}（{provider_id}）" if model else provider_id,
            }
        )
    if choices:
        return choices, ""
    if failed:
        return [], "模型列表读取失败；当前已保存的模型 ID 保持不变。"
    return [], "当前没有可用的文本模型；留空时使用会话默认模型。"


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
                descriptor["configured"] = bool(
                    str(_effective(plugin, key, "") or "").strip()
                )
            elif key in SAFETY_KEYS or spec.get("type") == "template_list":
                descriptor["delegated"] = True
                descriptor["view"] = DELEGATED_VIEWS.get(key, "policies")
            elif spec.get("_special") == PROVIDER_SPECIAL:
                choices, warning = _provider_choices(plugin)
                current = str(_effective(plugin, key, "") or "").strip()
                if current and all(item["value"] != current for item in choices):
                    choices.insert(
                        0,
                        {
                            "value": current,
                            "label": f"{current}（当前已保存，未在模型列表中找到）",
                        },
                    )
                descriptor["special"] = PROVIDER_SPECIAL
                descriptor["options"] = choices
                descriptor["default"] = deepcopy(spec.get("default"))
                descriptor["value"] = current
                if warning:
                    descriptor["warning"] = warning
            else:
                descriptor["default"] = deepcopy(spec.get("default"))
                value = deepcopy(_effective(plugin, key, descriptor["default"]))
                if descriptor["type"] == "list":
                    if descriptor["options"]:
                        # 已保存的旧值原样返回供页面勾选保留；新写入仍只接受
                        # 声明选项或该键当前已存值，不把未知项扩成新选项。
                        descriptor["value"] = (
                            [item for item in value if isinstance(item, str)]
                            if isinstance(value, list)
                            else []
                        )
                    else:
                        # 没有可选清单的列表无法安全编辑，避免保存时被空值覆盖。
                        descriptor["editable"] = False
                        descriptor["value"] = value
                else:
                    descriptor["value"] = value
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


def _validate_value(
    key: str,
    value: Any,
    spec: dict[str, Any],
    allowed_extra: Sequence[str] = (),
) -> Any:
    label = spec.get("description", key)
    kind = spec.get("type")
    if key in SECRET_KEYS:
        if not isinstance(value, str):
            raise ValueError(f"{label}必须为文本")
        if not value.strip():
            # 空文本代表“保持不变”，清除必须走独立字段，避免误清已存令牌。
            raise ValueError(f"{label}留空表示保持不变；如需删除请勾选清除")
        return value.strip()
    if kind == "bool":
        valid = isinstance(value, bool)
    elif kind == "int":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif kind == "float":
        valid = (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    elif kind in {"string", "text"}:
        valid = isinstance(value, str)
    elif kind == "list":
        if not spec.get("options"):
            raise ValueError(f"{label}需在配置文件中维护，不能通过此设置页修改")
        valid = isinstance(value, list) and all(isinstance(item, str) for item in value)
    else:
        raise ValueError(f"{label}不能通过此设置页编辑")
    if not valid:
        raise ValueError(f"{label}类型不正确")
    if kind == "list":
        allowed = set(spec["options"]) | set(allowed_extra)
        invalid = [item for item in value if item not in allowed]
        if invalid:
            raise ValueError(f"{label}包含无效选项")
    if "options" in spec and kind != "list" and value not in spec["options"]:
        raise ValueError(f"{label}不在可选范围内")
    slider = spec.get("slider")
    if slider and kind in {"int", "float"}:
        if value < slider["min"] or value > slider["max"]:
            raise ValueError(f"{label}需在 {slider['min']} 至 {slider['max']} 之间")
        # 与页面同语义：步长相对 slider.min，容浮点误差；旧值未改动不会走到这里。
        step = slider.get("step")
        if isinstance(step, (int, float)) and not isinstance(step, bool) and step:
            base = slider.get("min", 0)
            ratio = (value - base) / step
            if not math.isclose(ratio, round(ratio), rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"{label}需为 {base} 起每 {step} 的倍数")
    return value


def _normalize_clear_secrets(clear_secrets: Any) -> list[str]:
    if clear_secrets is None:
        return []
    if isinstance(clear_secrets, str) or not isinstance(clear_secrets, (list, tuple)):
        raise ValueError("清除令牌的请求格式不正确")
    cleared: list[str] = []
    for key in clear_secrets:
        if not isinstance(key, str) or key not in SECRET_KEYS:
            raise ValueError("只能清除本页支持的令牌项")
        if key not in cleared:
            cleared.append(key)
    return cleared


def save_settings(plugin: Any, patch: Any, clear_secrets: Any = ()) -> None:
    if not isinstance(patch, dict):
        raise ValueError("设置内容格式不正确")
    cleared = _normalize_clear_secrets(clear_secrets)
    if not patch and not cleared:
        raise ValueError("设置内容格式不正确")
    schema = load_schema()
    groups = _visible_groups(schema)
    secret_groups = _secret_groups(schema)
    validated: dict[str, dict[str, Any]] = {}
    for group_key, values in patch.items():
        if group_key not in groups or not isinstance(values, dict) or not values:
            raise ValueError("设置分组不正确")
        specs = groups[group_key]["items"]
        for key, value in values.items():
            spec = specs.get(key)
            if (
                not spec
                or spec.get("invisible")
                or spec.get("type") == "template_list"
                or key in SAFETY_KEYS
            ):
                raise ValueError("包含不能在此页面编辑的设置项")
            # 多选旧值不在声明选项里时，只放行该键当前已保存的字符串值；
            # 用户不能借旧值通道注入任意新值。
            allowed_extra: tuple[str, ...] = ()
            if spec.get("type") == "list" and spec.get("options"):
                stored = _effective(plugin, key, spec.get("default"))
                if isinstance(stored, list):
                    allowed_extra = tuple(
                        item for item in stored if isinstance(item, str)
                    )
            validated.setdefault(group_key, {})[key] = _validate_value(
                key, value, spec, allowed_extra
            )
    for key in cleared:
        if key not in secret_groups:
            raise ValueError("只能清除本页支持的令牌项")
        if any(key in values for values in validated.values()):
            raise ValueError("同一个令牌不能同时替换和清除")
        validated.setdefault(secret_groups[key], {})[key] = ""

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
        # 同步短临界区：不跨 await、不自动重载，失败整体回滚。
        config.save_config()
    except Exception:
        for group_key, original in originals.items():
            group, had_group, root_had_group, root_group_value, old_values = original
            for key, (present, original_value, old_value) in old_values.items():
                if present:
                    if isinstance(original_value, list) and isinstance(old_value, list):
                        original_value[:] = old_value
                    elif isinstance(original_value, dict) and isinstance(
                        old_value, dict
                    ):
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
    logger.info(
        f"{LOG_PREFIX} 管理页设置已保存: "
        f"groups={','.join(sorted(validated)) or 'none'} "
        f"fields={sum(len(values) for values in validated.values())} "
        f"cleared_secrets={len(cleared)}"
    )
