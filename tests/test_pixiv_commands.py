"""明确 Pixiv 指令与模型工具共用领域执行、安全、顺序和结算。"""

from __future__ import annotations

from pathlib import Path
import sys
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.pixiv.client import PixivPage
from astrbot_plugin_get_px.pixiv.commands import PixivCommandMixin, parse_command
from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.query import QueryError
from test_p_coin_cost import _illust
from test_public_discovery import flow


@pytest.mark.parametrize(
    "mode,text,expected,count",
    [
        ("auto", "白子 蔚蓝档案 2", ("白子 蔚蓝档案",), 2),
        ("auto", "洛茜 白丝", ("洛茜 白丝",), 1),
        ("artwork", "42", (42, 1), 1),
        ("artwork", "https://www.pixiv.net/artworks/42 2", (42, 2), 1),
        ("ranking", "周 3", ("week",), 3),
        ("ranking", "MONTH", ("month",), 1),
    ],
)
def test_command_text_preserves_conditions_and_page_semantics(mode, text, expected, count):
    query, actual_count = parse_command(mode, text)
    assert actual_count == count and query.mode == mode
    if mode == "auto":
        assert (query.tag,) == expected
    elif mode == "artwork":
        assert (query.targets[0].illust_id, query.targets[0].page) == expected
    else:
        assert (query.ranking_mode,) == expected


@pytest.mark.parametrize("mode,text", [
    ("auto", "白子 0"),
    ("auto", "名" * 49), ("artwork", ""),
    ("artwork", "https://example.com/artworks/42"),
    ("artwork", "42 0"), ("artwork", "42 1 2"),
    ("ranking", ""), ("ranking", "热门 2"), ("ranking", "周 -1"),
])
def test_invalid_command_arguments_are_rejected_before_execution(mode, text):
    with pytest.raises(QueryError):
        parse_command(mode, text)


def test_native_commands_register_greedy_queries():
    found = {}
    for handler in star_handlers_registry.get_handlers_by_event_type(EventType.AdapterMessageEvent):
        if "astrbot_plugin_get_px" not in str(handler.handler_module_path):
            continue
        if handler.handler_name not in {"cmd_p", "cmd_pd", "cmd_pr"}:
            continue
        command = next(f for f in handler.event_filters if isinstance(f, CommandFilter))
        found[handler.handler_name] = command.get_complete_command_names()
        assert command.handler_params["query"] is GreedyStr
        assert command.validate_and_convert_params(
            ["白子", "蔚蓝档案", "2"], command.handler_params
        )["query"] == "白子 蔚蓝档案 2"
    assert found == {"cmd_p": ["p"], "cmd_pd": ["pd"], "cmd_pr": ["pr"]}


async def command_flow(tmp_path, items=3):
    plugin, context, paths, service = await flow(tmp_path, items=items)
    plugin._handle_pixiv_command = PixivCommandMixin._handle_pixiv_command.__get__(plugin)
    event = context.context.event
    event.stop_event = Mock()
    plugin._ensure_client_or_error = lambda event: True
    return plugin, event, service


async def collect(plugin, event, method, text=GreedyStr):
    return [reply async for reply in method(plugin, event, text)]


@pytest.mark.asyncio
@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("method,text,mode,expected", [
    (GetPxPlugin.cmd_p, "白子 蔚蓝档案 2", "auto", "白子 蔚蓝档案"),
    (GetPxPlugin.cmd_pr, "周 2", "ranking", "week"),
])
async def test_command_modes_share_selection_billing_and_forward_configuration(
    tmp_path, method, text, mode, expected, forward
):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.client.query_page.return_value = PixivPage([_illust(3), _illust(1), _illust(2)])
    plugin._fetch_source_candidates = AsyncMock(return_value=([_illust(3), _illust(1), _illust(2)], 3, "pixiv:search:fixture"))
    plugin._check_rate_limit = Mock(return_value=0)
    plugin._forward_threshold = lambda: 0 if forward else 5
    if mode == "auto":
        plugin._pick_illusts = AsyncMock(side_effect=lambda event, items, amount, **kwargs: items[:amount])
    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)) as sender:
        assert not await collect(plugin, event, method, text)
    if mode == "auto":
        assert plugin._fetch_source_candidates.await_args.args[1] == expected
    else:
        query = plugin.client.query_page.await_args.args[0]
        assert query.mode == mode and query.ranking_mode == expected
    assert [call.args[0]["id"] for call in plugin.downloader.download_for_send.await_args_list] == ["3", "1"]
    assert sender.await_count == (1 if forward else 2)
    assert (await plugin.checkin_store.find_profile("10001")).coins == 60
    assert any("40" in str(message) for message in event.sent)
    if mode != "auto":
        plugin._fetch_source_candidates.assert_not_awaited()
    plugin._check_rate_limit.assert_called_once()
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
async def test_download_command_selects_only_requested_page_and_allows_completed_history(tmp_path):
    plugin, event, _ = await command_flow(tmp_path, items=1)
    plugin.client.public_detail.side_effect = None
    plugin.client.public_detail.return_value = dict(_illust(42), page_count=2, meta_pages=[
        {"image_urls": {"original": "https://i.pximg.net/first.jpg"}},
        {"image_urls": {"original": "https://i.pximg.net/second.jpg"}},
    ])
    plugin.image_index = ImageIndexStore(tmp_path)
    await plugin.image_index.record_usage(
        scope=plugin._event_scope(event), source_key="pixiv:artwork",
        illust_id="42:1", feature="normal",
    )
    try:
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)) as sender:
            assert not await collect(plugin, event, GetPxPlugin.cmd_pd, "42 2")
        item = plugin.downloader.download_for_send.await_args.args[0]
        assert item["id"] == "42:1" and item["image_urls"]["original"].endswith("second.jpg")
        assert sender.await_count == 1 and (await plugin.checkin_store.find_profile("10001")).coins == 80
        plugin.client.query_page.assert_not_awaited()
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_source", "balance", "rate_limit", "blocked_query"])
async def test_command_refusals_do_not_query_download_or_charge(tmp_path, case):
    plugin, event, _ = await command_flow(tmp_path)
    text = "白子"
    if case == "missing_source":
        plugin._ensure_client_or_error = lambda event: False
    elif case == "empty_query":
        text = GreedyStr
    elif case == "balance":
        await plugin.checkin_store.spend_coins(user_id="10001", cost=100)
    elif case == "rate_limit":
        plugin._check_rate_limit = Mock(return_value=10)
    else:
        plugin._blocked_query_term = AsyncMock(return_value="blocked")
    before = (await plugin.checkin_store.find_profile("10001")).coins
    assert await collect(plugin, event, GetPxPlugin.cmd_p, text)
    assert (await plugin.checkin_store.find_profile("10001")).coins == before
    plugin.client.query_page.assert_not_awaited()
    plugin.downloader.download_for_send.assert_not_awaited()
    event.stop_event.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["not_found", "filtered", "page_unavailable", "download_failed", "inflight"])
async def test_explicit_failures_do_not_substitute_or_charge(tmp_path, case):
    plugin, event, _ = await command_flow(tmp_path)
    text = "1"
    if case == "not_found":
        plugin.client.public_detail.side_effect = None
        plugin.client.public_detail.return_value = None
    elif case == "filtered":
        plugin.client.public_detail.side_effect = None
        plugin.client.public_detail.return_value = dict(_illust(1), x_restrict=1)
    elif case == "page_unavailable":
        text = "1 2"
    elif case == "download_failed":
        plugin.downloader.download_for_send.side_effect = OSError("fixture download failure")
    else:
        plugin.image_index = ImageIndexStore(tmp_path)
        await plugin.image_index.claim_usage(
            scope=plugin._event_scope(event), source_key="pixiv:artwork:explicit_pending",
            illust_id="1", feature="normal_pending",
        )
    try:
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock()) as sender:
            assert await collect(plugin, event, GetPxPlugin.cmd_pd, text)
        sender.assert_not_awaited()
        plugin.client.query_page.assert_not_awaited()
        assert (await plugin.checkin_store.find_profile("10001")).coins == 100
        if case != "download_failed":
            plugin.downloader.download_for_send.assert_not_awaited()
    finally:
        if plugin.image_index is not None:
            plugin.image_index.close()


@pytest.mark.asyncio
async def test_explicit_search_upstream_error_does_not_fall_back(tmp_path):
    plugin, event, _ = await command_flow(tmp_path)
    plugin.client.query_page.side_effect = OSError("secret fixture")
    plugin._fetch_source_candidates = AsyncMock()
    replies = await collect(plugin, event, GetPxPlugin.cmd_pr, "周")
    assert replies and "secret fixture" not in str(replies)
    plugin._fetch_source_candidates.assert_not_awaited()
    plugin.downloader.download_for_send.assert_not_awaited()
    assert (await plugin.checkin_store.find_profile("10001")).coins == 100
