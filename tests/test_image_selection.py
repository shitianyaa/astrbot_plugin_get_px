"""Offline regressions for bounded source selection and real continuation."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.client import PixivClient
from astrbot_plugin_get_px.pixiv.lolicon import LoliconClient
from astrbot_plugin_get_px.pixiv.search import SearchMixin
from astrbot_plugin_get_px.pixiv.pagination import (
    SourceRequest,
    PixivPage,
    SelectionIndexError,
)
from test_p_coin_cost import (
    _SearchFlowHarness,
    _FlowEvent,
    _illust as flow_illust,
    _run_search,
)


def _illust(ident):
    return {**flow_illust(ident), "type": "illust"}


def client_with_response(payload):
    client = PixivClient("fixture")
    client._api = AsyncMock()
    client._api.search_illust.return_value = payload
    client._api.illust_recommended.return_value = payload
    client._cached_token = "fixture"
    client._expires_at = float("inf")
    return client


@pytest.mark.asyncio
async def test_pixiv_recommendation_preserves_zero_offset_and_bookmark_cursor():
    client = client_with_response(
        {
            "illusts": [_illust(1)],
            "next_url": "https://app-api.pixiv.net/v1/illust/recommended?offset=0&max_bookmark_id_for_recommend=42",
        }
    )
    page = await client.recommended()
    assert page.next_params == {"offset": 0, "max_bookmark_id_for_recommend": 42}
    await client.recommended(continuation=page.next_params)
    client._api.illust_recommended.assert_awaited_with(
        offset=0, max_bookmark_id_for_recommend=42
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"illusts": None},
        {"illusts": {}},
        {"error": {"message": "private"}},
        {"illusts": [{"id": 1}]},
    ],
)
async def test_pixiv_rejects_malformed_lists(payload):
    with pytest.raises(RuntimeError):
        await client_with_response(payload).search("tag")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/v1/search/illust?offset=20",
        "https://app-api.pixiv.net/v1/illust/recommended?offset=20",
        "https://app-api.pixiv.net/v1/search/illust?offset=-1",
        "https://app-api.pixiv.net/v1/search/illust?offset=20&token=secret",
    ],
)
async def test_pixiv_rejects_untrusted_continuation(url):
    with pytest.raises(RuntimeError):
        await client_with_response({"illusts": [], "next_url": url}).search("tag")


@pytest.mark.asyncio
async def test_pixiv_portrait_search_uses_authenticated_existing_request():
    client = client_with_response({"illusts": [], "next_url": None})
    client._api.requests_.return_value = {"illusts": [], "next_url": None}
    await client.search("tag", portrait=True)
    kwargs = client._api.requests_.await_args.kwargs
    assert kwargs["url"] == "https://app-api.pixiv.net/v1/search/illust"
    assert kwargs["params"]["ratio_pattern"] == "portrait"
    assert kwargs["auth"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["manga", "safety", "used"])
async def test_first_filtered_batch_replenishes(stage):
    harness = _SearchFlowHarness()
    first = _illust(1)
    harness._filter_manga = lambda items: (
        [] if stage == "manga" and items == [first] else items
    )
    harness._filter_blacklisted_illusts = AsyncMock(
        side_effect=lambda items, policy: (
            [] if stage == "safety" and items == [first] else items
        )
    )
    harness._pick_illusts = AsyncMock(
        side_effect=lambda event, items, n, **kwargs: (
            [] if stage == "used" and items == [first] else items[:n]
        )
    )
    harness._fetch_source_candidates = AsyncMock(
        side_effect=[
            ([first], 1, "lolicon:random"),
            ([_illust(2)], 1, "lolicon:random"),
        ]
    )
    harness.downloader = AsyncMock()
    harness.downloader.download_for_send.return_value = ("unused.jpg", "original", 10)
    await _run_search(harness, _FlowEvent(), "1")
    assert harness._fetch_source_candidates.await_count == 2
    assert harness.downloader.download_for_send.await_args.args[0]["id"] == "2"


@pytest.mark.asyncio
async def test_lolicon_rejects_missing_data_and_invalid_rating():
    client = LoliconClient()
    response = AsyncMock()
    response.status = 200
    response.__aenter__.return_value = response
    session = AsyncMock()
    session.closed = False
    session.get = lambda *args, **kwargs: response
    client._session = session
    for payload in ({}, {"data": [{"pid": 1, "p": 0, "r18": "false"}]}):
        response.json.return_value = payload
        with pytest.raises(RuntimeError):
            await client.random()


@pytest.mark.asyncio
async def test_source_request_keeps_empty_pixiv_continuation_and_lolicon_unpaged():
    from types import SimpleNamespace

    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client = SimpleNamespace(
        recommended=AsyncMock(
            side_effect=[
                PixivPage([], {"offset": 0, "max_bookmark_id_for_recommend": 42}),
                PixivPage([_illust(2)], None),
            ]
        )
    )
    source.lolicon_client = SimpleNamespace(
        available=True, random=AsyncMock(return_value=[_illust(1)])
    )
    source.image_index = SimpleNamespace(set_page_offset=AsyncMock())
    state = SourceRequest()
    first = await source._fetch_source_candidates(
        _FlowEvent(), "", request_state=state, use_page_cursor=False
    )
    assert first[2] == "lolicon:random"
    second = await source._fetch_source_candidates(
        _FlowEvent(), "", request_state=state, use_page_cursor=False
    )
    assert second[2] == "pixiv:recommended"
    assert (
        source.client.recommended.await_args.kwargs["continuation"][
            "max_bookmark_id_for_recommend"
        ]
        == 42
    )
    await source._advance_source_page(_FlowEvent(), "lolicon:random", state)
    source.image_index.set_page_offset.assert_not_awaited()
    assert "offset" not in source.lolicon_client.random.await_args.kwargs


@pytest.mark.asyncio
async def test_repeated_pixiv_cursor_is_not_requested_twice():
    from types import SimpleNamespace

    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client = SimpleNamespace(
        recommended=AsyncMock(return_value=PixivPage([_illust(1)], {"offset": 0}))
    )
    source.lolicon_client = None
    source.image_index = None
    state = SourceRequest()
    await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    assert not (
        await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    )[0]
    await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    assert source.client.recommended.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["safety", "index"])
async def test_local_service_failure_stops_replenishment(stage):
    harness = _SearchFlowHarness(illusts=[_illust(1)])
    harness._fetch_source_candidates = AsyncMock(
        return_value=(harness.illusts, 1, "lolicon:random")
    )
    harness.downloader = AsyncMock()
    if stage == "safety":
        harness._filter_blacklisted_illusts = AsyncMock(
            side_effect=RuntimeError("fixture")
        )
    else:
        harness._pick_illusts = AsyncMock(side_effect=SelectionIndexError("fixture"))
    result = await _run_search(harness, _FlowEvent(), "1")
    assert "服务暂不可用" in result[-1]
    assert harness._fetch_source_candidates.await_count == 1
    harness.downloader.download_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_selection_waits_for_sqlite_claim_then_releases(tmp_path):
    import asyncio
    from types import MethodType
    from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    source = object.__new__(SearchMixin)
    index = ImageIndexStore(tmp_path)
    source.image_index = index
    source._pick_illusts = MethodType(FiltersMixin._pick_illusts, source)
    started, complete = asyncio.Event(), asyncio.Event()
    claim = index.claim_usage

    async def delayed_claim(**kwargs):
        started.set()
        await complete.wait()
        return await claim(**kwargs)

    index.claim_usage = delayed_claim
    task = asyncio.create_task(
        source._pick_illusts(_FlowEvent(), [_illust(1)], 1, source_key="lolicon:random")
    )
    try:
        await started.wait()
        task.cancel()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            await index.get_used_illust_ids(
                source._event_scope(_FlowEvent()), "lolicon:random"
            )
            == set()
        )
    finally:
        index.close()


@pytest.mark.asyncio
async def test_last_unusable_pixiv_batch_persists_real_next_offset(tmp_path):
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    harness = _SearchFlowHarness()
    index = ImageIndexStore(tmp_path)
    harness.image_index = index
    harness._dedupe_days_cfg = 1
    calls = 0

    async def fetch(event, tag, *, request_state, **kwargs):
        nonlocal calls
        calls += 1
        request_state.next_params["pixiv:recommended"] = {"offset": calls * 7}
        return [], 0, "pixiv:recommended"

    harness._fetch_source_candidates = fetch
    harness.downloader = AsyncMock()
    try:
        await _run_search(harness, _FlowEvent(), "1")
        assert calls == 3
        assert (
            await index.get_page_offset(
                harness._event_scope(_FlowEvent()), "pixiv:recommended"
            )
            == 21
        )
    finally:
        index.close()


@pytest.mark.asyncio
async def test_checkin_claim_failure_is_not_a_duplicate():
    from types import SimpleNamespace
    from astrbot_plugin_get_px.main import GetPxPlugin

    source = object.__new__(GetPxPlugin)
    source.image_index = SimpleNamespace(
        claim_usage=AsyncMock(side_effect=OSError("fixture"))
    )
    with pytest.raises(SelectionIndexError):
        await source._claim_checkin_background_usage(
            _FlowEvent(), "pixiv:recommended", "1"
        )


@pytest.mark.asyncio
async def test_cancelled_checkin_claim_releases_late_write(tmp_path):
    import asyncio
    from astrbot_plugin_get_px.main import GetPxPlugin
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    source = object.__new__(GetPxPlugin)
    index = ImageIndexStore(tmp_path)
    source.image_index = index
    started, complete = asyncio.Event(), asyncio.Event()
    claim = index.claim_usage

    async def delayed_claim(**kwargs):
        started.set()
        await complete.wait()
        return await claim(**kwargs)

    index.claim_usage = delayed_claim
    task = asyncio.create_task(
        source._claim_checkin_background_usage(_FlowEvent(), "pixiv:recommended", "1")
    )
    try:
        await started.wait()
        task.cancel()
        complete.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            await index.get_used_illust_ids(
                source._event_scope(_FlowEvent()), "pixiv:recommended"
            )
            == set()
        )
    finally:
        index.close()


@pytest.mark.asyncio
async def test_pixiv_empty_page_and_viewed_indices_are_valid():
    client = client_with_response(
        {
            "illusts": [],
            "next_url": "https://app-api.pixiv.net/v1/illust/recommended?offset=0&max_bookmark_id_for_recommend=42&viewed%5B0%5D=7",
        }
    )
    page = await client.recommended()
    assert page.items == []
    assert page.next_params == {"offset": 0, "max_bookmark_id_for_recommend": 42, "viewed": [7]}


@pytest.mark.asyncio
async def test_lolicon_empty_and_error_responses_are_distinct():
    client = LoliconClient()
    response = AsyncMock()
    response.status = 200
    response.__aenter__.return_value = response
    session = AsyncMock()
    session.closed = False
    session.get = lambda *args, **kwargs: response
    client._session = session
    response.json.return_value = {"error": "", "data": []}
    assert await client.random() == []
    response.json.return_value = {"error": "private upstream details", "data": []}
    with pytest.raises(RuntimeError) as error:
        await client.random()
    assert "private" not in str(error.value)
