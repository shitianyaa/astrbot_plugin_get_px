"""Pixiv 分页协议、请求回放与响应安全边界（全程离线）。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pixivpy_async import AppPixivAPI

from test_pixiv_client import client_with_response
from test_p_coin_cost import _FlowEvent
from astrbot_plugin_get_px.pixiv.pagination import (
    SourceRequest,
    SourceResponseError,
    parse_continuation,
    parse_pixiv_page,
)
from astrbot_plugin_get_px.pixiv.search import SearchMixin
from astrbot_plugin_get_px.pixiv.downloader import pick_image_url_exact

BASE = "https://app-api.pixiv.net/v1/illust/recommended?"
ITEM = {"id": 1, "type": "illust", "x_restrict": 0, "tags": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected",
    [
        (
            "min_bookmark_id_for_recent_illust=0&max_bookmark_id_for_recommend=-1",
            {
                "min_bookmark_id_for_recent_illust": 0,
                "max_bookmark_id_for_recommend": -1,
            },
        ),
        (
            "min_bookmark_id_for_recent_illust=0&max_bookmark_id_for_recommend=0",
            {
                "min_bookmark_id_for_recent_illust": 0,
                "max_bookmark_id_for_recommend": 0,
            },
        ),
        (
            "min_bookmark_id_for_recent_illust=7&max_bookmark_id_for_recommend=42",
            {
                "min_bookmark_id_for_recent_illust": 7,
                "max_bookmark_id_for_recommend": 42,
            },
        ),
        (
            "viewed[1]=8&viewed[0]=7&bookmark_illust_ids[]=9,10",
            {"viewed": [7, 8], "bookmark_illust_ids": [9, 10]},
        ),
        (
            "viewed[]=7&viewed[]=8&bookmark_illust_ids[]=9&bookmark_illust_ids[]=10",
            {"viewed": [7, 8], "bookmark_illust_ids": [9, 10]},
        ),
    ],
)
async def test_recommendation_continuation_replays_and_stops_cycles(query, expected):
    expected = {"offset": 0, **expected}
    client = client_with_response(
        {"illusts": [ITEM], "next_url": BASE + "offset=0&" + query}
    )
    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client, source.lolicon_client, source.image_index = client, None, None
    state = SourceRequest()
    for _ in range(2):
        assert (
            await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
        )[0]
    client._api.illust_recommended.assert_awaited_with(**expected)
    assert not (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    assert client._api.illust_recommended.await_count == 2

    # 检查第三方SDK边界，requests_替身阻止任何HTTP请求。
    api = AppPixivAPI()
    api.requests_ = AsyncMock()
    await api.illust_recommended(**expected)
    encoded = api.requests_.await_args.kwargs["params"]
    for key, value in expected.items():
        if key == "viewed":
            assert [encoded[f"viewed[{i}]"] for i in range(len(value))] == value
        elif key == "bookmark_illust_ids":
            assert encoded["bookmark_illust_ids[]"] == ",".join(map(str, value))
        else:
            assert encoded[key] == value


@pytest.mark.parametrize(
    "value",
    [
        123,
        "https://[private-host/v1/illust/recommended?offset=1",
        "https://user-secret@example.org/v1/illust/recommended?offset=1",
        "http://app-api.pixiv.net/v1/illust/recommended?offset=1",
        "https://app-api.pixiv.net/v1/search/illust?offset=1",
        *[
            BASE + query
            for query in (
                "offset=1#private-fragment",
                "private-token-name=private-value",
                "offset=1&offset=2",
                "offset=-1",
                "min_bookmark_id_for_recent_illust=-1",
                "max_bookmark_id_for_recommend=-2",
                "max_bookmark_id_for_recommend=-01",
                "max_bookmark_id_for_recommend=--1",
                "max_bookmark_id_for_recommend=1.0",
                "max_bookmark_id_for_recommend=",
                "include_privacy_policy=PrivateBool",
                "filter=PrivateFilter",
                "viewed[0]=7&viewed[0]=8",
                "viewed[1]=7",
                "viewed[0]=7&viewed[]=8",
                "viewed[0]=7&viewed[00]=8",
                "viewed[]=0",
                "viewed[]=-1",
                "viewed[]=1,2",
                "bookmark_illust_ids[]=7,,8",
                "bookmark_illust_ids[]=private",
                "bookmark_illust_ids[]=-1",
                "offset",
                "",
            )
        ],
    ],
)
def test_untrusted_continuation_is_rejected_without_echoing_input(value):
    with pytest.raises(SourceResponseError) as caught:
        parse_continuation(value, recommended=True)
    assert caught.value.reason == "invalid_continuation"
    assert "private" not in str(caught.value).lower()
    assert "https://" not in str(caught.value)


@pytest.mark.parametrize(
    "query", ["word=private-search", "max_bookmark_id_for_recommend=-1"]
)
def test_search_continuation_cannot_change_query_or_use_recommendation_parameters(
    query,
):
    with pytest.raises(SourceResponseError) as caught:
        parse_continuation(
            "https://app-api.pixiv.net/v1/search/illust?offset=0&" + query,
            recommended=False,
            tag="expected-search",
        )
    assert "private-search" not in str(caught.value)


@pytest.mark.parametrize(
    "next_url, expected",
    [
        (None, None),
        ("", None),
        (
            BASE + "min_bookmark_id_for_recent_illust=0",
            {"min_bookmark_id_for_recent_illust": 0},
        ),
        (
            BASE + "max_bookmark_id_for_recommend=-1",
            {"max_bookmark_id_for_recommend": -1},
        ),
        (BASE + "viewed[0]=7", {"viewed": [7]}),
    ],
)
def test_empty_page_preserves_continuation_or_end(next_url, expected):
    page = parse_pixiv_page({"illusts": [], "next_url": next_url}, recommended=True)
    assert page.items == [] and page.next_params == expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"illusts": None},
        {"illusts": {}},
        {"illusts": [{"id": 1}]},
        {"error": {"message": "private"}},
        *[
            {"illusts": [{**ITEM, **update}]}
            for update in (
                {"id": 0},
                {"id": -1},
                {"x_restrict": None},
                {"x_restrict": "0"},
                {"tags": None},
                {"meta_pages": {}},
            )
        ],
    ],
)
def test_malformed_page_or_candidate_is_rejected(payload):
    with pytest.raises(SourceResponseError):
        parse_pixiv_page(payload, recommended=True)


@pytest.mark.parametrize("rating", [0, 1, 2])
def test_multipage_and_missing_quality_are_left_for_filtering_and_downloading(rating):
    item = {
        **ITEM,
        "x_restrict": rating,
        "tags": [{"name": "tag", "translated_name": None}],
        "meta_single_page": {},
        "image_urls": {},
        "meta_pages": [{"image_urls": {"original": "https://i.pximg.net/sample.jpg"}}],
    }
    assert parse_pixiv_page({"illusts": [item]}, recommended=True).items == [item]
    assert pick_image_url_exact(item, "large") == ""
    assert pick_image_url_exact(item, "original") == "https://i.pximg.net/sample.jpg"


@pytest.mark.asyncio
async def test_invalid_source_continuation_falls_back_without_leaking_secrets():
    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client = client_with_response(
        {"illusts": [], "next_url": BASE + "private-token-name=private-value"}
    )
    source.image_index = None
    source.lolicon_client = SimpleNamespace(
        available=True, random=AsyncMock(return_value=[ITEM])
    )
    with patch("astrbot_plugin_get_px.pixiv.search.logger") as logger:
        items, _, key = await source._fetch_source_candidates(_FlowEvent(), "")
    assert key == "lolicon:random" and items == [ITEM]
    text = logger.warning.call_args.args[0]
    for forbidden in ("private-token-name", "private-value", BASE):
        assert forbidden not in text
