"""Pixiv 公开查询输入与固定 SDK 分页边界。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_p_coin_cost import _illust
from astrbot_plugin_get_px.pixiv.client import PixivClient, parse_continuation
from astrbot_plugin_get_px.pixiv.query import ArtworkQuery, QueryError


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "ranking", "tag": "bad"},
        {"mode": "search", "tag": ""},
        {"mode": "search", "tag": "x", "sort": "popular_desc"},
        {"mode": "ranking", "date": "2026-02-30"},
        {"mode": "ranking", "date": "2026-2-3"},
        {
            "mode": "search",
            "tag": "x",
            "start_date": "2026-02-02",
            "end_date": "2026-01-01",
        },
        {"mode": "author", "user_id": True},
        {"mode": "artwork", "targets": [{"artwork": "https://evil.test/artworks/42"}]},
        {"mode": "artwork", "targets": [{"artwork": "42", "page": 0}]},
    ],
)
def test_query_contract_rejects_mismatched_or_unsafe_input(args):
    with pytest.raises(QueryError):
        ArtworkQuery.from_args(args)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.test/v1/search/illust?offset=1",
        "http://app-api.pixiv.net/v1/search/illust?offset=1",
        "https://app-api.pixiv.net/v1/user/illusts?offset=1",
        "https://app-api.pixiv.net/v1/search/illust?offset=-1",
    ],
)
def test_upstream_cursor_cannot_select_arbitrary_endpoint(url):
    with pytest.raises(QueryError):
        parse_continuation(
            url, "https://app-api.pixiv.net/v1/search/illust", {"offset"}
        )


@pytest.mark.asyncio
async def test_sdk_page_dispatch_and_public_field_boundary(tmp_path):
    client = PixivClient("fixture")
    from pixivpy_async import AppPixivAPI

    sdk = AppPixivAPI()
    api = SimpleNamespace(
        api=sdk.api,
        illust_recommended=AsyncMock(
            return_value={
                "illusts": [_illust(1)],
                "next_url": "https://app-api.pixiv.net/v1/illust/recommended?offset=30&content_type=illust",
            }
        ),
    )
    client._api, client._cached_token, client._expires_at = api, "fixture", float("inf")
    result = await client.query_page(ArtworkQuery(mode="recommended"))
    assert result.next_params == {"offset": 30}
    api.illust_recommended.return_value = {"error": {"message": "secret upstream text"}}
    with pytest.raises(QueryError, match="响应结构"):
        await client.query_page(ArtworkQuery(mode="recommended"))
    assert client._active_calls == 0

    # 真实关联续页使用索引数组，SDK 高层方法未提供 viewed 参数。
    api.illust_related = AsyncMock(return_value={
        "illusts": [_illust(2)],
        "next_url": "https://app-api.pixiv.net/v2/illust/related?illust_id=42&filter=for_ios&seed_illust_ids[0]=42&viewed[0]=2",
    })
    api.requests_ = AsyncMock(return_value={"illusts": [_illust(3)]})
    query = ArtworkQuery(mode="related", illust_id=42)
    first = await client.query_page(query)
    assert first.items[0]["id"] == "2"
    assert first.next_params == {"seed_illust_ids": [42], "viewed": [2]}
    second = await client.query_page(query, first.next_params)
    assert second.items[0]["id"] == "3" and second.next_params is None
    assert api.requests_.await_args.kwargs == {
        "method": "GET", "url": "https://app-api.pixiv.net/v2/illust/related",
        "params": {"illust_id": 42, "filter": "for_ios", "seed_illust_ids[0]": 42, "viewed[0]": 2},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,method",
    [
        (
            ArtworkQuery(
                mode="search",
                tag="x",
                search_target="exact_match_for_tags",
                sort="date_asc",
            ),
            "search_illust",
        ),
        (
            ArtworkQuery(mode="ranking", ranking_mode="week", date="2026-11-01"),
            "illust_ranking",
        ),
        (ArtworkQuery(mode="author", user_id=12), "user_illusts"),
        (ArtworkQuery(mode="related", illust_id=42), "illust_related"),
    ],
)
async def test_new_client_modes_dispatch_only_to_fixed_sdk_methods(query, method):
    from pixivpy_async import AppPixivAPI

    api = SimpleNamespace(api=AppPixivAPI().api)
    setattr(api, method, AsyncMock(return_value={"illusts": []}))
    client = PixivClient("fixture")
    client._api, client._cached_token, client._expires_at = api, "fixture", float("inf")
    result = await client.query_page(query, {"offset": 30})
    assert result.items == [] and result.next_params is None
    args = getattr(api, method).await_args.kwargs
    assert args["offset"] == 30
    if query.mode == "search":
        assert (
            args["search_target"] == "exact_match_for_tags"
            and args["sort"] == "date_asc"
        )
