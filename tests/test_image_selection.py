"""双图源补选、请求内分页状态与未提交占用清理。"""

import asyncio
import sys
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.pixiv.search import SearchMixin
from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.pagination import (
    SourceRequest,
    PixivPage,
    SelectionIndexError,
)
from test_p_coin_cost import _SearchFlowHarness, _FlowEvent, _illust, _run_search


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["manga", "safety", "used"])
async def test_filtered_batch_replenishes(stage):
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
async def test_empty_pixiv_page_retains_continuation_across_source_fallback():
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
    assert source.client.recommended.await_args.kwargs["continuation"] == {
        "offset": 0,
        "max_bookmark_id_for_recommend": 42,
    }
    await source._advance_source_page(_FlowEvent(), "lolicon:random", state)
    source.image_index.set_page_offset.assert_not_awaited()
    assert "offset" not in source.lolicon_client.random.await_args.kwargs


@pytest.mark.asyncio
async def test_repeated_pixiv_cursor_is_not_requested_twice():
    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client = SimpleNamespace(
        recommended=AsyncMock(return_value=PixivPage([_illust(1)], {"offset": 0}))
    )
    source.lolicon_client = None
    source.image_index = None
    state = SourceRequest()
    await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
    for _ in range(2):
        assert not (
            await source._fetch_source_candidates(_FlowEvent(), "", request_state=state)
        )[0]
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
    assert await _run_search(harness, _FlowEvent(), "1")
    harness._fetch_source_candidates.assert_awaited_once()
    harness.downloader.download_for_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("feature", ["normal", "checkin"])
async def test_cancelled_selection_waits_for_sqlite_claim_then_releases(
    tmp_path, feature
):
    source = object.__new__(GetPxPlugin)
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
    selection = (
        source._claim_checkin_background_usage(_FlowEvent(), "lolicon:random", "1")
        if feature == "checkin"
        else source._pick_illusts(
            _FlowEvent(), [_illust(1)], 1, source_key="lolicon:random"
        )
    )
    task = asyncio.create_task(selection)
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
async def test_unusable_final_batch_persists_upstream_position(tmp_path):
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
