"""Request-local continuation for Pixiv; Lolicon has no paged stream."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from urllib.parse import parse_qs, urlsplit


class SourceResponseError(RuntimeError):
    """Safe source failure without upstream response text."""

    def __init__(self, reason: str, *, detail: str = "none", field: str = "none"):
        self.reason = reason
        self.detail = detail
        self.field = field
        super().__init__(f"图片源响应不可用: reason={reason}")


def validate_pixiv_candidate(item: object) -> None:
    if (
        not isinstance(item, dict)
        or isinstance(item.get("id"), bool)
        or not str(item.get("id", "")).isdecimal()
        or int(item["id"]) <= 0
        or type(item.get("x_restrict")) is not int
        or item["x_restrict"] not in {0, 1, 2}
        or not isinstance(item.get("type"), str)
        or not isinstance(item.get("tags"), list)
        or any(
            not isinstance(t, dict) or not isinstance(t.get("name"), str)
            for t in item.get("tags", [])
        )
        or not isinstance(item.get("user", {}), dict)
        or not isinstance(item.get("image_urls", {}), dict)
        or not isinstance(item.get("meta_single_page", {}), dict)
        or not isinstance(item.get("meta_pages", []), list)
    ):
        raise SourceResponseError("malformed_candidate")
    containers = [item.get("image_urls", {})]
    if any(
        not isinstance(url, str)
        for key, url in item.get("meta_single_page", {}).items()
        if key.endswith("url")
    ):
        raise SourceResponseError("malformed_candidate")
    for page in item.get("meta_pages", []):
        if not isinstance(page, dict) or not isinstance(
            page.get("image_urls", {}), dict
        ):
            raise SourceResponseError("malformed_candidate")
        containers.append(page.get("image_urls", {}))
    if any(
        not isinstance(url, str)
        for urls in containers
        for key, url in urls.items()
        if key not in {"width", "height"}
    ):
        raise SourceResponseError("malformed_candidate")


class SelectionIndexError(RuntimeError):
    """Local dedupe failure must not be treated as an exhausted candidate pool."""


@dataclass
class PixivPage:
    items: list[dict]
    next_params: dict | None


@dataclass
class SourceRequest:
    next_params: dict[str, dict | None] = field(default_factory=dict)
    seen: dict[str, set[tuple]] = field(default_factory=dict)
    failure_reason: str = ""


def parse_continuation(
    value: object, *, recommended: bool, tag: str = ""
) -> dict | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SourceResponseError("invalid_continuation", detail="value_type")
    path = "/v1/illust/recommended" if recommended else "/v1/search/illust"
    detail, field = "url_syntax", "none"
    try:
        url = urlsplit(value)
        detail = "scheme"
        if url.scheme != "https":
            raise ValueError
        detail = "host"
        if url.netloc != "app-api.pixiv.net":
            raise ValueError
        detail = "path"
        if url.path != path:
            raise ValueError
        detail = "fragment"
        if url.fragment:
            raise ValueError
        detail = "query_syntax"
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        numbers = {"offset"}
        booleans = set()
        static = (
            {"filter", "content_type", "include_ranking_label"}
            if recommended
            else {"word", "search_target", "sort", "filter", "ratio_pattern"}
        )
        if recommended:
            numbers.update(
                {"max_bookmark_id_for_recommend", "min_bookmark_id_for_recent_illust"}
            )
            booleans.update({"include_ranking_illusts", "include_privacy_policy"})
        params = {}
        indexed_viewed = {}
        for key, values in query.items():
            if recommended and (
                re.fullmatch(r"viewed\[\d*\]", key) or key == "bookmark_illust_ids[]"
            ):
                field = "bookmark_illust_ids" if key == "bookmark_illust_ids[]" else "viewed"
                detail = "list_number"
                ids = [value for raw in values for value in raw.split(",")]
                if any(not value.isdecimal() or int(value) <= 0 for value in ids):
                    raise ValueError
                if key == "bookmark_illust_ids[]":
                    params["bookmark_illust_ids"] = [int(value) for value in ids]
                elif key == "viewed[]":
                    detail = "list_format"
                    if any("," in raw for raw in values):
                        raise ValueError
                    params["viewed"] = [int(value) for value in ids]
                else:
                    detail = "list_format"
                    if len(values) != 1 or "," in values[0]:
                        raise ValueError
                    detail = "list_index"
                    index = int(key[7:-1])
                    if len(ids) != 1 or index in indexed_viewed:
                        raise ValueError
                    indexed_viewed[index] = int(ids[0])
                continue
            detail, field = "unknown_parameter", "unknown"
            if key not in numbers | booleans | static:
                raise ValueError
            # Only fixed allowlisted names reach diagnostics; never echo an unknown key.
            detail, field = "scalar_count", key
            if len(values) != 1:
                raise ValueError
            raw = values[0]
            if key in numbers:
                detail = "number"
                # 推荐边界不是作品 ID；上游可返回 min=0 / max=-1 哨兵。
                max_sentinel = recommended and key == "max_bookmark_id_for_recommend" and raw == "-1"
                if not raw.isdecimal() and not max_sentinel:
                    raise ValueError
                params[key] = int(raw)
            elif key in booleans:
                detail = "boolean"
                if raw not in {"true", "false", "0", "1"}:
                    raise ValueError
                params[key] = raw in {"true", "1"}
            elif key in static:
                detail = "static_value"
                expected = {
                    "word": {tag},
                    "search_target": {"partial_match_for_tags"},
                    "sort": {"date_desc"},
                    "filter": {"for_ios", "for_android"},
                    "ratio_pattern": {"portrait"},
                    "content_type": {"illust"},
                    "include_ranking_label": {"true", "false", "0", "1"},
                }
                if raw not in expected[key]:
                    raise ValueError
            else:
                raise ValueError
        if indexed_viewed:
            detail, field = "list_indices", "viewed"
            if "viewed" in params or sorted(indexed_viewed) != list(
                range(len(indexed_viewed))
            ):
                raise ValueError
            params["viewed"] = [
                indexed_viewed[index] for index in range(len(indexed_viewed))
            ]
        if not params:
            detail, field = "empty_parameters", "none"
            raise ValueError
        return params
    except (ValueError, UnicodeError) as exc:
        raise SourceResponseError("invalid_continuation", detail=detail, field=field) from exc


def parse_pixiv_page(payload: object, *, recommended: bool, tag: str = "") -> PixivPage:
    if not isinstance(payload, dict):
        raise SourceResponseError("malformed_response")
    if payload.get("error"):
        raise SourceResponseError("upstream_error")
    items = payload.get("illusts")
    if not isinstance(items, list):
        raise SourceResponseError("malformed_response")
    for item in items:
        validate_pixiv_candidate(item)
    return PixivPage(
        items,
        parse_continuation(payload.get("next_url"), recommended=recommended, tag=tag),
    )
