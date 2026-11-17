"""固定Pixiv接口的续页字段契约，供响应解析、SDK调用和持久化共用。"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from .query import QueryError


_EXTRA_FIELDS = {
    "illust_recommended": {
        "max_bookmark_id_for_recommend": "cursor",
        "min_bookmark_id_for_recent_illust": "cursor",
        "viewed": "ids",
        "bookmark_illust_ids": "ids",
        "include_ranking_label": "bool",
        "include_ranking_illusts": "bool",
        "include_privacy_policy": "bool",
    },
    "illust_related": {"seed_illust_ids": "ids", "viewed": "ids"},
}


def _fields(method: str) -> dict[str, str]:
    return {"offset": "integer", **_EXTRA_FIELDS.get(method, {})}


def _integer(value: object) -> int:
    if type(value) is int and 0 <= value < 10**20:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        return int(value)
    raise ValueError("分页整数无效")


def validate_continuation(method: str, params: object) -> dict:
    """归一化字段类型；推荐书签位置是有界游标，不假定都是作品整数ID。"""
    fields = _fields(method)
    if not isinstance(params, dict) or not params or set(params) - fields.keys():
        raise ValueError("分页参数与接口不匹配")
    result = {}
    for key, value in params.items():
        kind = fields[key]
        if kind == "ids":
            if not isinstance(value, list) or not 1 <= len(value) <= 1024:
                raise ValueError("分页作品列表无效")
            result[key] = [_integer(item) for item in value]
        elif kind == "cursor" and isinstance(value, str) and not re.fullmatch(r"[0-9]{1,20}", value):
            if not 1 <= len(value) <= 256 or not value.isascii() or not value.isprintable():
                raise ValueError("分页游标无效")
            result[key] = value
        elif kind == "bool":
            if type(value) is bool:
                result[key] = value
            elif isinstance(value, str) and value.casefold() in ("true", "false"):
                result[key] = value.casefold() == "true"
            else:
                raise ValueError("分页显示标记无效")
        else:
            result[key] = _integer(value)
    if not any(fields[key] != "bool" for key in result):
        raise ValueError("图片源未返回续页位置")
    return result


def _array_values(params: dict, key: str) -> list[str] | None:
    indexed = []
    for name, values in params.items():
        if name == key + "[]" or not name.startswith(key + "["):
            continue
        match = re.fullmatch(re.escape(key) + r"\[([0-9]{1,4})\]", name)
        if not match or len(values) != 1:
            raise ValueError("分页数组下标无效")
        indexed.append((int(match[1]), values[0]))
    plain = [params[name] for name in (key, key + "[]") if name in params]
    if indexed:
        indexed.sort()
        if plain or len(indexed) > 1024 or any(index != position for position, (index, _) in enumerate(indexed)):
            raise ValueError("分页数组下标不连续或形式混合")
        return [value for _, value in indexed]
    if len(plain) > 1:
        raise ValueError("分页数组形式混合")
    # SDK也会将种子／书签列表编码成逗号分隔的field[]。
    return [item for value in plain[0] for item in value.split(",")] if plain else None


def parse_continuation(value: object, endpoint: str, method: str) -> dict | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise QueryError("upstream_error", "图片源返回的续页信息无效。")
    try:
        parsed, expected = urlsplit(value), urlsplit(endpoint)
        if (parsed.scheme != "https" or parsed.netloc != expected.netloc
                or parsed.path != expected.path or parsed.fragment):
            raise ValueError("续页地址与接口不匹配")
        params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4096)
        result = {}
        for key, kind in _fields(method).items():
            values = _array_values(params, key) if kind == "ids" else params.get(key)
            if values is None:
                continue
            if kind == "ids":
                result[key] = values
            else:
                if len(values) != 1:
                    raise ValueError("分页单值字段重复")
                if kind == "cursor" and values[0] == "":
                    continue
                result[key] = values[0]
        return validate_continuation(method, result)
    except ValueError as exc:
        raise QueryError("upstream_error", "图片源返回的续页参数或地址无效。") from exc
