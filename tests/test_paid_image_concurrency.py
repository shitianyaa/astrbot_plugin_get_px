"""收费取图在共享执行入口按钱包用户协调，不排队、不预扣。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_image_tools import Event
from test_pixiv_commands import command_flow, collect
from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore


def context(event):
    return SimpleNamespace(context=SimpleNamespace(event=event), tool_call_timeout=120)


async def invoke(plugin, service, event, entry):
    if entry == "tool":
        return await service.image(context(event), {"tag": "cat"})
    return await collect(plugin, event, GetPxPlugin.cmd_p, "cat")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_entry,second_entry", [("tool", "tool"), ("command", "tool"), ("tool", "command")])
async def test_paid_overlap_is_busy_before_query_and_does_not_consume_capacity(tmp_path, first_entry, second_entry):
    plugin, event, service = await command_flow(tmp_path)
    plugin._fetch_source_candidates = AsyncMock(wraps=plugin._fetch_source_candidates)
    await plugin.checkin_store.spend_coins(user_id="10001", cost=80)
    other = Event()
    other.stop_event = lambda: None
    started, release = asyncio.Event(), asyncio.Event()

    async def deliver(*args, **kwargs):
        started.set()
        await release.wait()
        return DeliveryStatus.CONFIRMED

    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", side_effect=deliver) as sender:
        task = asyncio.create_task(invoke(plugin, service, event, first_entry))
        try:
            await asyncio.wait_for(started.wait(), 2)
            queries = plugin._fetch_source_candidates.await_count
            downloads = plugin.downloader.download_for_send.await_count
            busy = await asyncio.wait_for(invoke(plugin, service, other, second_entry), 2)
            assert "收费取图正在执行" in str(busy)
            assert plugin.downloader.download_for_send.await_count == downloads
            assert plugin._fetch_source_candidates.await_count == queries
            if second_entry == "tool":
                assert not busy["retry_allowed"] and not busy["delivery"]
                assert other.get_extra("get_px_tool_requests").used == 0
            assert (await plugin.checkin_store.find_profile("10001")).coins == 20
        finally:
            release.set()
            await task
        assert sender.await_count == 1
    assert not plugin._paid_image_users
    assert (await plugin.checkin_store.find_profile("10001")).coins == 0
    later = await service.image(context(Event()), {"tag": "other"})
    assert "金币" in later["message"] and "正在执行" not in later["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["free", "other_user"])
async def test_free_and_other_user_requests_do_not_share_busy_state(tmp_path, case):
    plugin, event, service = await command_flow(tmp_path, items=2)
    plugin.image_index = ImageIndexStore(tmp_path)
    other = Event()
    if case == "free":
        plugin.config["p_coin_cost"] = 0
    else:
        other.get_sender_id = lambda: "10002"
        await plugin.checkin_store.get_profile("10002")
        await plugin.checkin_store.update_checkin_member(user_id="10002", coins=100, affection=0, total_days=0, streak_days=0)
    count = 0
    both = asyncio.Event()

    async def deliver(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 2)
        return DeliveryStatus.CONFIRMED

    try:
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", side_effect=deliver):
            outcomes = await asyncio.gather(service.image(context(event), {}), service.image(context(other), {}))
        assert count == 2 and all(result["delivery"] == {"confirmed": 1} for result in outcomes)
        assert (await plugin.checkin_store.find_profile("10001")).coins == (100 if case == "free" else 80)
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
async def test_busy_flag_lasts_through_repeated_cancellation_of_settlement(tmp_path):
    plugin, event, service = await command_flow(tmp_path, items=1)
    started, release = asyncio.Event(), asyncio.Event()
    spend = plugin.checkin_store.spend_coins

    async def settle(**kwargs):
        started.set()
        await release.wait()
        return await spend(**kwargs)

    plugin.checkin_store.spend_coins = settle
    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)):
        task = asyncio.create_task(service.image(context(event), {}))
        try:
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            busy = await service.image(context(Event()), {})
            assert not busy["delivery"] and "正在执行" in busy["message"]
            assert plugin._paid_image_users == {"10001"}
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert not plugin._paid_image_users
    assert (await plugin.checkin_store.find_profile("10001")).coins == 80
