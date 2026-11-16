"""公开热度查询的三个业务回归场景。"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from test_public_discovery import flow, next_cursor
from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus


@pytest.mark.asyncio
async def test_stats_are_opt_in_and_survive_cursors_without_side_effects(tmp_path):
    plugin, context, _, service = await flow(tmp_path, items=4)
    stats = service.discovery.stats_client
    stats.fetch = AsyncMock(return_value={"total_likes": 12, "total_bookmarks": 30, "total_view": 200})
    before = (await plugin.checkin_store.find_profile("10001")).coins
    plain = await service.query(context, "artworks", {"mode": "ranking", "limit": 1})
    assert "total_likes" not in plain["records"][0]
    stats.fetch.assert_not_awaited()
    first = await service.query(context, "artworks", {"mode": "ranking", "limit": 2, "include_stats": True})
    second = await service.query(context, "artworks", {"cursor": next_cursor(first)})
    assert [r["id"] for r in first["records"] + second["records"]] == ["1", "2", "3", "4"]
    assert all(r["total_likes"] == 12 for r in second["records"])
    assert await service.query(context, "artworks", {"cursor": next_cursor(first)}) == second
    assert stats.fetch.await_count == 4
    assert (await plugin.checkin_store.find_profile("10001")).coins == before
    plugin.downloader.download_for_send.assert_not_awaited()
    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)):
        result = await service.image(context, {"mode": "artwork", "targets": [{"artwork": "2"}]})
    assert result["downloaded_count"] == 1 and result["settlement"] == "completed"
    assert stats.fetch.await_count == 4
    assert (await plugin.checkin_store.find_profile("10001")).coins == before - 20
    await service.close()
    assert stats._closed


@pytest.mark.asyncio
async def test_stats_failures_preserve_candidates_known_counts_and_no_send(tmp_path, monkeypatch):
    plugin, context, _, service = await flow(tmp_path, items=2)
    plugin.client.query_page.return_value.items[0].update(total_view=90, total_bookmarks=6)
    async def enrich(records):
        await asyncio.Event().wait()
    monkeypatch.setattr(service.discovery.stats_client, "enrich", enrich)
    monkeypatch.setattr("astrbot_plugin_get_px.pixiv.discovery.STATS_TIMEOUT", 0.01)
    result = await service.query(context, "artworks", {"mode": "ranking", "include_stats": True})
    assert result["status"] == "ok" and len(result["records"]) == 2
    first, second = result["records"]
    assert first["total_likes"] is None and first["total_bookmarks"] == 6 and first["total_view"] == 90
    assert second["total_likes"] is None and second["total_view"] is None
    plugin.downloader.download_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_shutdown_cancels_enrichment_and_waits_before_closing_session(tmp_path):
    _, context, _, service = await flow(tmp_path)
    session = service.discovery.stats_client._ensure_session()
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def wait(records):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    service.discovery.stats_client.enrich = wait
    task = asyncio.create_task(service.query(context, "artworks", {"mode": "ranking", "include_stats": True}))
    await started.wait()
    await service.close()
    assert task.cancelled() and cancelled.is_set()
    assert service.discovery.stats_client._closed and session.closed
