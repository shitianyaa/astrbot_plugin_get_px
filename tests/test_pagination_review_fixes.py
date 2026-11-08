"""Offline regressions for continuation lists and explicit portrait routing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_image_selection import client_with_response, _illust
from test_p_coin_cost import _FlowEvent
from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.pixiv.pagination import (
    PixivPage,
    SourceRequest,
    SourceResponseError,
    parse_continuation,
)
from astrbot_plugin_get_px.pixiv.search import SearchMixin


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected",
    [
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
async def test_recommendation_lists_are_replayed_and_cycle_identity_is_hashable(
    query, expected
):
    client = client_with_response(
        {
            "illusts": [_illust(1)],
            "next_url": f"https://app-api.pixiv.net/v1/illust/recommended?offset=0&{query}",
        }
    )
    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client = client
    source.lolicon_client = None
    source.image_index = None
    state = SourceRequest()
    await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    assert state.next_params["pixiv:recommended"] == {"offset": 0, **expected}
    assert (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    client._api.illust_recommended.assert_awaited_with(offset=0, **expected)
    from pixivpy_async import AppPixivAPI

    api = AppPixivAPI()
    api.requests_ = AsyncMock()
    await api.illust_recommended(**expected)
    params = api.requests_.await_args.kwargs["params"]
    assert params["viewed[0]"] == 7
    assert params["viewed[1]"] == 8
    assert params["bookmark_illust_ids[]"] == "9,10"
    assert not (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    assert client._api.illust_recommended.await_count == 2


@pytest.mark.parametrize(
    "query",
    [
        "viewed[0]=7&viewed[0]=8",
        "viewed[1]=7",
        "viewed[0]=7&viewed[]=8",
        "viewed[0]=7&viewed[00]=8",
        "viewed[]=0",
        "bookmark_illust_ids[]=7,,8",
        "bookmark_illust_ids[]=private",
    ],
)
def test_invalid_recommendation_lists_are_rejected(query):
    with pytest.raises(SourceResponseError, match="invalid_continuation"):
        parse_continuation(
            f"https://app-api.pixiv.net/v1/illust/recommended?offset=0&{query}",
            recommended=True,
        )


@pytest.mark.asyncio
async def test_checkin_portrait_routing_does_not_depend_on_lolicon_ratio_string():
    source = object.__new__(GetPxPlugin)
    source.config = {"preferred_image_source": "Pixiv", "checkin_background_tag": "tag"}
    source.image_index = None
    source.lolicon_client = None
    source.client = SimpleNamespace(
        search=AsyncMock(
            return_value=PixivPage([{**_illust(1), "width": 750, "height": 1000}], None)
        )
    )
    source._checkin_background_used_ids = AsyncMock(return_value=set())
    source._filter_blacklisted_illusts = AsyncMock(
        side_effect=lambda items, policy: items
    )
    source._blacklist_reason_for_illust = AsyncMock(return_value="")
    source.downloader = SimpleNamespace(
        download_for_send=AsyncMock(return_value=("unused.jpg", "large", 1))
    )
    with patch(
        "astrbot_plugin_get_px.checkin.artwork.CHECKIN_ARTWORK_ASPECT_PARAM",
        "gt0.55lt0.95",
    ):
        assert (
            await source._download_checkin_pixiv_background(
                _FlowEvent(),
                SimpleNamespace(date_key="2026-11-08", user_id="1"),
                claim_usage=False,
            )
            is not None
        )
    assert source.client.search.await_args.kwargs["portrait"] is True
    # A ratio filter alone must not enable Pixiv portrait mode for other callers.
    await source._fetch_source_candidates(
        _FlowEvent(), "tag", aspect_ratio="gt0.60lt0.90"
    )
    assert source.client.search.await_args.kwargs["portrait"] is False
