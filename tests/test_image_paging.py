"""普通/p沿真实游标续页，保持来源与条件，不将空批误判成去重耗尽。"""

from unittest.mock import AsyncMock, patch
import pytest

from test_pixiv_commands import command_flow, collect
from test_p_coin_cost import _illust
from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.pixiv.client import PixivPage
from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.search import SearchMixin


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["empty", "filtered", "used"])
async def test_p_continues_when_first_batch_has_no_usable_image(tmp_path, first):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.config["preferred_image_source"] = "Pixiv"
    plugin._fetch_source_candidates = SearchMixin._fetch_source_candidates.__get__(plugin)
    plugin.image_index = ImageIndexStore(tmp_path)
    if first == "used":
        await plugin.image_index.record_usage(scope=plugin._event_scope(event), source_key="other-entry", illust_id="1", feature="normal")
    items = [] if first == "empty" else [dict(_illust(1), x_restrict=1 if first == "filtered" else 0)]
    plugin.client.query_page.side_effect = [PixivPage(items, {"offset": 30}), PixivPage([_illust(2)])]
    try:
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)) as sender:
            assert not await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜 黑丝")
        assert plugin.client.query_page.await_count == 2
        assert plugin.client.query_page.await_args_list[1].args[1] == {"offset": 30}
        assert plugin.client.query_page.await_args_list[1].args[0].tag == "洛茜 黑丝"
        assert [c.args[0]["id"] for c in plugin.downloader.download_for_send.await_args_list] == ["2"]
        assert sender.await_count == 1
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
async def test_no_cursor_stops_and_empty_reply_does_not_claim_history_exhaustion(tmp_path):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.config["preferred_image_source"] = "Pixiv"
    plugin._fetch_source_candidates = SearchMixin._fetch_source_candidates.__get__(plugin)
    plugin.client.query_page.return_value = PixivPage([])
    reply = await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜 黑丝")
    assert reply and "没有未发送过" not in str(reply)
    assert plugin.client.query_page.await_count == 1
    plugin.downloader.download_for_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_command_and_tool_different_conditions_share_recent_history(tmp_path):
    plugin, event, service = await command_flow(tmp_path)
    plugin.image_index = ImageIndexStore(tmp_path)
    plugin._fetch_source_candidates = AsyncMock(return_value=([_illust(1)], 1, "lolicon:search:first"))
    plugin.client.query_page.return_value = PixivPage([dict(_illust(1), id="1:0")])
    try:
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)) as sender:
            await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜")
            from types import SimpleNamespace
            context = SimpleNamespace(context=SimpleNamespace(event=event), tool_call_timeout=120)
            result = await service.image(context, {"mode": "search", "tag": "洛茜 白丝"})
        assert not result["delivery"] and sender.await_count == 1
        assert (await plugin.checkin_store.find_profile("10001")).coins == 80
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
async def test_stale_start_rechecks_home_once_without_changing_checkin_cursor(tmp_path):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.config["preferred_image_source"] = "Pixiv"
    plugin._fetch_source_candidates = SearchMixin._fetch_source_candidates.__get__(plugin)
    plugin.image_index = ImageIndexStore(tmp_path)
    scope, key = plugin._event_scope(event), plugin._source_key("洛茜 黑丝", "pixiv")
    await plugin.image_index.advance_page_offset(scope, key, 30)
    await plugin.image_index.advance_page_offset(scope, key, 60, feature="checkin")
    plugin.client.query_page.side_effect = [PixivPage([]), PixivPage([_illust(1)])]
    try:
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)) as sender:
            assert not await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜 黑丝")
        assert [call.args[1] for call in plugin.client.query_page.await_args_list] == [{"offset": 30}, {"offset": 0}]
        sender.assert_awaited_once()
        assert await plugin.image_index.get_page_offset(scope, key) == 0
        assert await plugin.image_index.get_page_offset(scope, key, feature="checkin") == 60
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
async def test_empty_home_stops_retry_and_next_message_starts_at_zero(tmp_path):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.config["preferred_image_source"] = "Pixiv"
    plugin._fetch_source_candidates = SearchMixin._fetch_source_candidates.__get__(plugin)
    plugin.image_index = ImageIndexStore(tmp_path)
    scope, key = plugin._event_scope(event), plugin._source_key("洛茜 黑丝", "pixiv")
    await plugin.image_index.advance_page_offset(scope, key, 30)
    plugin.client.query_page.return_value = PixivPage([])
    try:
        first = await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜 黑丝")
        assert plugin.client.query_page.await_count == 2
        assert "图片源没有返回候选" in str(first) and "去重" not in str(first)
        await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜 黑丝")
        assert [call.args[1] for call in plugin.client.query_page.await_args_list] == [{"offset": 30}, {"offset": 0}, {"offset": 0}]
        plugin.downloader.download_for_send.assert_not_awaited()
        assert (await plugin.checkin_store.find_profile("10001")).coins == 100
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
async def test_home_retry_counts_towards_the_existing_batch_budget(tmp_path):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.config["preferred_image_source"] = "Pixiv"
    plugin._fetch_source_candidates = SearchMixin._fetch_source_candidates.__get__(plugin)
    plugin.image_index = ImageIndexStore(tmp_path)
    scope, key = plugin._event_scope(event), plugin._source_key("洛茜 黑丝", "pixiv")
    await plugin.image_index.advance_page_offset(scope, key, 30)
    filtered = dict(_illust(1), x_restrict=1)
    plugin.client.query_page.side_effect = [
        PixivPage([]), PixivPage([filtered], {"offset": 30}),
        PixivPage([filtered], {"offset": 60}), PixivPage([_illust(2)]),
    ]
    try:
        await collect(plugin, event, GetPxPlugin.cmd_p, "洛茜 黑丝")
        assert plugin.client.query_page.await_count == 3
        plugin.downloader.download_for_send.assert_not_awaited()
    finally:
        plugin.image_index.close()
