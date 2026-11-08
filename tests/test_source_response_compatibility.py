"""Keep pagination sentinels separate from offsets, ratings and artwork IDs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_image_selection import _illust, client_with_response
from test_p_coin_cost import _FlowEvent
from astrbot_plugin_get_px.pixiv.pagination import (
    SourceRequest,
    SourceResponseError,
    parse_continuation,
    parse_pixiv_page,
)
from astrbot_plugin_get_px.pixiv.search import SearchMixin
from astrbot_plugin_get_px.pixiv.downloader import pick_image_url_exact
from astrbot_plugin_get_px.pixiv.lolicon import LoliconClient

BASE = "https://app-api.pixiv.net/v1/illust/recommended?"


@pytest.mark.asyncio
@pytest.mark.parametrize("minimum, maximum", [(0, -1), (0, 0), (7, 42)])
async def test_recommended_boundaries_are_replayed_without_reset(minimum, maximum):
    client = client_with_response(
        {
            "illusts": [_illust(1)],
            "next_url": BASE
            + f"offset=0&min_bookmark_id_for_recent_illust={minimum}&max_bookmark_id_for_recommend={maximum}",
        }
    )
    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client, source.lolicon_client, source.image_index = client, None, None
    state = SourceRequest()
    assert (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    expected = {
        "offset": 0,
        "min_bookmark_id_for_recent_illust": minimum,
        "max_bookmark_id_for_recommend": maximum,
    }
    assert state.next_params["pixiv:recommended"] == expected
    assert (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    client._api.illust_recommended.assert_awaited_with(**expected)
    # Verify real installed SDK serialization without making an HTTP request.
    from pixivpy_async import AppPixivAPI

    api = AppPixivAPI()
    api.requests_ = AsyncMock()
    await api.illust_recommended(**expected)
    params = api.requests_.await_args.kwargs["params"]
    assert {key: params[key] for key in expected} == expected
    assert not (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    assert client._api.illust_recommended.await_count == 2


@pytest.mark.parametrize(
    "query",
    [
        "offset=-1",
        "min_bookmark_id_for_recent_illust=-1",
        "max_bookmark_id_for_recommend=-2",
        "max_bookmark_id_for_recommend=-01",
        "max_bookmark_id_for_recommend=--1",
        "max_bookmark_id_for_recommend=1.0",
        "max_bookmark_id_for_recommend=",
        "viewed[]=0",
        "viewed[]=-1",
        "bookmark_illust_ids[]=-1",
    ],
)
def test_sentinels_do_not_relax_other_numbers(query):
    with pytest.raises(SourceResponseError):
        parse_continuation(BASE + query, recommended=True)


@pytest.mark.parametrize(
    "query, expected",
    [
        (
            "min_bookmark_id_for_recent_illust=0",
            {"min_bookmark_id_for_recent_illust": 0},
        ),
        ("max_bookmark_id_for_recommend=-1", {"max_bookmark_id_for_recommend": -1}),
    ],
)
def test_empty_recommendation_page_keeps_boundary_without_offset(query, expected):
    page = parse_pixiv_page({"illusts": [], "next_url": BASE + query}, recommended=True)
    assert page.items == [] and page.next_params == expected


def test_search_does_not_accept_recommendation_sentinel():
    with pytest.raises(SourceResponseError):
        parse_continuation(
            "https://app-api.pixiv.net/v1/search/illust?offset=0&max_bookmark_id_for_recommend=-1",
            recommended=False,
        )


@pytest.mark.parametrize("next_url", [None, ""])
def test_empty_page_without_continuation_is_valid(next_url):
    page = parse_pixiv_page({"illusts": [], "next_url": next_url}, recommended=True)
    assert page.items == [] and page.next_params is None


def test_general_multipage_without_selected_quality_is_not_a_malformed_batch():
    item = {
        **_illust(1),
        "id": 1,
        "x_restrict": 0,
        "type": "illust",
        "tags": [{"name": "tag", "translated_name": None}],
        "meta_single_page": {},
        "image_urls": {},
        "meta_pages": [{"image_urls": {"original": "https://i.pximg.net/sample.jpg"}}],
    }
    page = parse_pixiv_page({"illusts": [item], "next_url": None}, recommended=True)
    assert page.items == [item]
    assert pick_image_url_exact(item, "large") == ""
    assert pick_image_url_exact(item, "original") == "https://i.pximg.net/sample.jpg"


@pytest.mark.parametrize("rating", [0, 1, 2])
def test_valid_ratings_are_left_for_policy_filtering(rating):
    item = {**_illust(1), "x_restrict": rating}
    assert parse_pixiv_page(
        {"illusts": [item], "next_url": None}, recommended=True
    ).items == [item]


@pytest.mark.parametrize(
    "updates",
    [
        {"id": 0},
        {"id": -1},
        {"x_restrict": None},
        {"x_restrict": "0"},
        {"tags": None},
        {"meta_pages": {}},
    ],
)
def test_malformed_candidate_still_rejects_batch(updates):
    with pytest.raises(SourceResponseError):
        parse_pixiv_page(
            {"illusts": [{**_illust(1), **updates}], "next_url": None}, recommended=True
        )


@pytest.mark.asyncio
async def test_lolicon_page_zero_empty_tags_and_missing_quality_are_valid():
    response = AsyncMock()
    response.status = 200
    response.__aenter__.return_value = response
    response.json.return_value = {
        "error": "",
        "data": [{"pid": 1, "p": 0, "r18": False, "tags": [], "aiType": 0, "urls": {}}],
    }
    client = LoliconClient()
    client._session = SimpleNamespace(
        closed=False, get=lambda *args, **kwargs: response
    )
    items = await client.random()
    assert items[0]["id"] == "1:0" and items[0]["x_restrict"] == 0
    assert pick_image_url_exact(items[0], "large") == ""
