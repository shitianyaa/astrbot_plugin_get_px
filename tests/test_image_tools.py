"""原生工具的消费、结果与生命周期边界；网络和平台发送留在测试侧。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_get_px.tools.images import (
    ImageToolService,
    GetPxImageTool,
    GetPxContextTool,
)
from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus
from astrbot_plugin_get_px.pixiv.safety import ContentSafetyPolicy
from astrbot_plugin_get_px.checkin import CheckinStore
from test_p_coin_cost import _SearchFlowHarness, _FlowEvent, _illust


class Event(_FlowEvent):
    def __init__(self):
        super().__init__("aiocqhttp")
        self.unified_msg_origin = "bot-a:GroupMessage:1"
        self.bot = SimpleNamespace()
        self.extras = {}

    def get_extra(self, key):
        return self.extras.get(key)

    def set_extra(self, key, value):
        self.extras[key] = value


async def setup_flow(tmp_path, coins=100, items=3):
    plugin = _SearchFlowHarness("aiocqhttp", [_illust(i + 1) for i in range(items)])
    plugin._cfg_int = lambda key, default, lo, hi: int(plugin.config.get(key, default))
    plugin._content_safety_policy = AsyncMock(return_value=ContentSafetyPolicy())
    plugin.checkin_store = CheckinStore(tmp_path)
    await plugin.checkin_store.get_profile("10001")
    await plugin.checkin_store.update_checkin_member(
        user_id="10001", coins=coins, affection=0, total_days=0, streak_days=0
    )
    paths = []
    for i in range(items):
        path = tmp_path / f"image-{i}.jpg"
        path.write_bytes(b"image")
        paths.append(str(path))
    plugin.downloader = SimpleNamespace(
        download_for_send=AsyncMock(side_effect=[(p, "original", 5) for p in paths]),
        defer_cleanup=Mock(),
    )
    manager = SimpleNamespace(func_list=[])
    plugin.context = SimpleNamespace(
        add_llm_tools=lambda *tools: manager.func_list.extend(tools),
        get_llm_tool_manager=lambda: manager,
    )
    context = SimpleNamespace(
        context=SimpleNamespace(event=Event()), tool_call_timeout=120
    )
    return plugin, context, paths


@pytest.mark.asyncio
async def test_tool_shrinks_to_affordable_and_never_emits_normal_bill(tmp_path):
    plugin, context, paths = await setup_flow(tmp_path, coins=45)
    service = ImageToolService(plugin)
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once",
        AsyncMock(return_value=DeliveryStatus.ACCEPTED),
    ):
        summary = await service.image(context, {"count": 3})
        repeated = await service.image(context, {"count": 2})
    assert summary["effective_count"] == 2 and summary["delivery"] == {"accepted": 2}
    assert summary["settlement"] == "completed" and summary["notice"]
    assert "balance" not in summary and "cost" not in summary
    assert not context.context.event.sent and repeated["duplicate_call"]
    assert not repeated["notice"]
    assert plugin.downloader.download_for_send.await_count == 2
    assert (await plugin.checkin_store.find_profile("10001")).coins == 5
    assert not summary["retry_allowed"]
    assert Path(paths[2]).exists()  # 未下载的测试输入不由执行器清理。


@pytest.mark.asyncio
async def test_low_balance_notice_uses_transaction_balance_and_frozen_price(tmp_path):
    plugin, context, _ = await setup_flow(tmp_path, coins=100)

    async def deliver(*args, **kwargs):
        plugin.config["p_coin_cost"] = 99
        return DeliveryStatus.CONFIRMED

    with patch("astrbot_plugin_get_px.pixiv.search.send_once", side_effect=deliver):
        summary = await ImageToolService(plugin).image(context, {"count": 3})
    assert (await plugin.checkin_store.find_profile("10001")).coins == 40
    assert summary["notice"] and summary["settlement"] == "completed"


@pytest.mark.asyncio
async def test_context_does_not_create_profiles_and_failures_are_not_free(tmp_path):
    plugin, context, _ = await setup_flow(tmp_path)
    context.context.event.get_sender_id = lambda: "new-user"
    service = ImageToolService(plugin)
    snapshot = await service.get_context(context, {})
    assert snapshot["balance"] == 0 and snapshot["affordable_count"] == 0
    assert await plugin.checkin_store.find_profile("new-user") is None
    plugin.checkin_store.find_profile = AsyncMock(side_effect=OSError("secret"))
    snapshot = json.loads(await GetPxContextTool(service).call(context))
    assert snapshot["message"] and "charging" not in snapshot
    assert "secret" not in json.dumps(snapshot)
    summary = await service.image(context, {"count": 1})
    assert summary["message"] and summary["downloaded_count"] == 0
    assert "secret" not in json.dumps(summary)
    plugin.checkin_store = None
    context.context.event = Event()
    snapshot = json.loads(await GetPxContextTool(service).call(context))
    assert snapshot["message"] and "charging" not in snapshot
    assert (await service.image(context, {}))["downloaded_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("checkin_enabled,cost", [(False, 20), (True, 0), (False, 0)])
@pytest.mark.parametrize("wallet_present", [True, False])
async def test_free_context_and_image_do_not_depend_on_wallet(
    tmp_path, checkin_enabled, cost, wallet_present
):
    plugin, context, _ = await setup_flow(tmp_path, coins=0, items=3)
    plugin.config.update(checkin_enabled=checkin_enabled, p_coin_cost=cost)
    wallet = plugin.checkin_store
    find_profile = wallet.find_profile
    wallet.find_profile = AsyncMock(side_effect=OSError("wallet unavailable"))
    wallet.get_profile = AsyncMock(side_effect=OSError("wallet unavailable"))
    wallet.spend_coins = AsyncMock(side_effect=OSError("wallet unavailable"))
    if not wallet_present:
        plugin.checkin_store = None
    service = ImageToolService(plugin)

    snapshot = json.loads(await GetPxContextTool(service).call(context))
    assert snapshot["charging"] is False and snapshot["unit_cost"] == 0
    assert snapshot["balance"] is None and snapshot["affordable_count"] is None
    assert snapshot["max_count"] == 5 and snapshot["allow_r18"] is False
    assert not service._calls
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ) as sender:
        summary = await service.image(context, {"count": 3})
    assert summary["effective_count"] == 3 and summary["delivery"] == {"confirmed": 3}
    assert summary["settlement"] == "not_required"
    assert not summary["message"] and not summary["notice"]
    assert not context.context.event.sent and sender.await_count == 3
    wallet.find_profile.assert_not_awaited()
    wallet.get_profile.assert_not_awaited()
    wallet.spend_coins.assert_not_awaited()
    assert (await find_profile("10001")).coins == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [
        {"count": True},
        {"count": 1.5},
        {"count": "2"},
        {"count": 0},
        {"tag": []},
        {"tag": "a\nb"},
        {"tag": "字" * 49},
        {"send_mode": True},
        {"send_mode": []},
        {"send_mode": "unknown"},
        {"orientation": "horizontal"},
        {"orientation": []},
        {"min_width": True},
        {"min_height": -1},
        {"min_width": 1000.0},
        {"aspect_ratio": "3:4"},
        {"aspect_ratio": None},
        {"aspect_ratio": 0},
        {"aspect_ratio": float("inf")},
        {"aspect_ratio": True},
        {"ratio_tolerance": 0.1},
        {"aspect_ratio": 0.75, "ratio_tolerance": -0.1},
        {"aspect_ratio": 0.75, "ratio_tolerance": 0.75},
        {"aspect_ratio": 0.75, "ratio_tolerance": float("nan")},
        {"max_size_mib": 0},
        {"max_size_mib": -1},
        {"max_size_mib": float("nan")},
        {"max_size_mib": "3"},
        {"max_size_mib": 1e-12},
        {"r18": True},
        {"target": "other"},
    ],
)
async def test_tool_rejects_invalid_parameters_before_consumption(tmp_path, args):
    plugin, context, _ = await setup_flow(tmp_path)
    service = ImageToolService(plugin)
    with pytest.raises(ValueError):
        await service.image(context, args)
    assert not context.context.event.extras
    plugin.downloader.download_for_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tag", ["洛茜", "ロージー", "Rosie", "名" * 48, ""])
async def test_tool_passes_short_names_without_expanding_or_truncating(tmp_path, tag):
    plugin, context, _ = await setup_flow(tmp_path, items=1)
    plugin._fetch_source_candidates = AsyncMock(wraps=plugin._fetch_source_candidates)
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ):
        summary = await ImageToolService(plugin).image(context, {"tag": tag})
    assert summary["delivery"] == {"confirmed": 1}
    assert plugin._fetch_source_candidates.await_args.args[1] == tag


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "send_mode,threshold,platform,forward",
    [
        (None, 0, "aiocqhttp", False),
        ("auto", 1, "aiocqhttp", True),
        ("auto", 2, "aiocqhttp", False),
        ("single", 0, "aiocqhttp", False),
        ("forward", 20, "aiocqhttp", True),
        ("forward", 0, "unsupported", False),
    ],
)
async def test_tool_send_mode_routes_without_changing_config_or_resending(
    tmp_path, send_mode, threshold, platform, forward
):
    from PIL import Image as PILImage

    plugin, context, paths = await setup_flow(tmp_path, items=2)
    for path in paths:
        PILImage.new("RGB", (8, 8)).save(path)
    plugin._forward_threshold = lambda: threshold
    event = context.context.event
    event.platform = platform
    event.get_group_id = lambda: "1"
    event.get_self_id = lambda: "10000"
    event.bot.call_action = AsyncMock(return_value={"message_id": 1})
    service = ImageToolService(plugin)
    args = {"count": 2}
    if send_mode is not None:
        args["send_mode"] = send_mode
    summary = await service.image(context, args)
    assert summary["delivery"] == {"confirmed": 2}
    assert summary["settlement"] == "completed"
    assert (await plugin.checkin_store.find_profile("10001")).coins == 60
    assert plugin._forward_threshold() == threshold
    if platform == "aiocqhttp":
        calls = event.bot.call_action.await_args_list
        assert len(calls) == (1 if forward else 2)
        assert all(
            call.args == ("send_group_forward_msg" if forward else "send_group_msg",)
            for call in calls
        )
        if forward:
            nodes = calls[0].kwargs["messages"]
            assert len(nodes) == 2
            assert all([part["type"] for part in node["data"]["content"]] == ["text", "image"] for node in nodes)
            image = nodes[0]["data"]["content"][1]
        else:
            image = calls[0].kwargs["message"][1]
        assert image["data"]["file"].startswith("file://")
        assert not event.sent
    else:
        event.bot.call_action.assert_not_awaited()
        assert len(event.sent) == 2
    repeated = await service.image(context, {"send_mode": "single"})
    assert repeated["duplicate_call"]
    assert plugin.downloader.download_for_send.await_count == 2
    assert plugin._forward_threshold() == threshold


@pytest.mark.asyncio
async def test_inflight_repeat_and_close_cancel_before_client_cleanup(tmp_path):
    plugin, context, _ = await setup_flow(tmp_path)
    started = asyncio.Event()
    finished = asyncio.Event()

    async def download(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    plugin.downloader.download_for_send.side_effect = download
    service = ImageToolService(plugin)
    service.register()
    foreign = Mock()
    plugin.context.get_llm_tool_manager().func_list.append(foreign)
    task = asyncio.create_task(service.image(context, {"count": 1}))
    await started.wait()
    assert (await service.image(context, {}))["duplicate_call"]
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and not service._calls
    assert plugin.context.get_llm_tool_manager().func_list == [foreign]
    assert context.context.event.get_extra(service.EVENT_KEY).interrupted
    assert not context.context.event.get_extra(service.EVENT_KEY).running
    with pytest.raises(ValueError):
        await service.image(context, {})


@pytest.mark.asyncio
async def test_actual_framework_executor_returns_text_and_does_not_resend(tmp_path):
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.agent.run_context import ContextWrapper

    from PIL import Image as PILImage

    plugin, context, paths = await setup_flow(tmp_path, coins=100, items=1)
    plugin.illusts[0].update(width=12, height=16)
    PILImage.new("RGB", (12, 16)).save(paths[0])
    service = ImageToolService(plugin)
    wrapper = ContextWrapper(context=context.context, tool_call_timeout=120)
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ) as sender:
        outputs = [
            r
            async for r in FunctionToolExecutor.execute(
                GetPxImageTool(service), wrapper, count=1, send_mode="single",
                orientation="portrait", aspect_ratio=0.75, ratio_tolerance=0,
                min_width=12, min_height=16, max_size_mib=3,
            )
        ]
    assert sender.await_count == 1 and not context.context.event.sent
    assert sender.await_args.kwargs["context"].startswith("send_mode=single")
    assert len(outputs) == 1
    summary = json.loads(outputs[0].content[0].text)
    assert summary["delivery"] == {"confirmed": 1}
    assert all(item.type == "text" for item in outputs[0].content)
    assert json.loads(await GetPxContextTool(service).call(wrapper))["balance"] == 80
    inactive = GetPxImageTool(service)
    inactive.active = False
    context.context.event = Event()
    assert "message" in json.loads(await inactive.call(wrapper))
    assert not context.context.event.extras


@pytest.mark.asyncio
@pytest.mark.parametrize("send_mode", ["single", "forward"])
@pytest.mark.parametrize(
    "status", [DeliveryStatus.TIMED_OUT, DeliveryStatus.UNKNOWN, DeliveryStatus.FAILED]
)
async def test_uncertain_or_failed_delivery_keeps_existing_charge_rules(
    tmp_path, status, send_mode
):
    plugin, context, _ = await setup_flow(tmp_path, coins=100, items=1)
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once", AsyncMock(return_value=status)
    ) as sender:
        service = ImageToolService(plugin)
        summary = await service.image(context, {"send_mode": send_mode})
        repeated = await service.image(
            context, {"send_mode": "forward" if send_mode == "single" else "single"}
        )
    assert repeated["duplicate_call"] and sender.await_count == 1
    charged = status is DeliveryStatus.TIMED_OUT
    assert (await plugin.checkin_store.find_profile("10001")).coins == (
        80 if charged else 100
    )
    assert summary["delivery"] == {status.value: 1}
    assert summary["settlement"] == ("completed" if charged else "not_required")
    assert not summary["retry_allowed"] and not context.context.event.sent


@pytest.mark.asyncio
async def test_settlement_shortfall_is_reported_without_amounts_or_resending(tmp_path):
    plugin, context, _ = await setup_flow(tmp_path, coins=100, items=1)

    async def deliver(*args, **kwargs):
        await plugin.checkin_store.spend_coins(user_id="10001", cost=100)
        return DeliveryStatus.CONFIRMED

    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once", side_effect=deliver
    ) as sender:
        summary = await ImageToolService(plugin).image(context, {})
    assert summary["settlement"] == "failed" and summary["message"]
    assert not context.context.event.sent and sender.call_count == 1
    assert "cost" not in summary and "balance" not in summary


@pytest.mark.asyncio
async def test_timeout_during_submit_remains_unknown_and_repeat_does_not_send(tmp_path):
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.agent.run_context import ContextWrapper

    plugin, context, paths = await setup_flow(tmp_path, items=1)
    service = ImageToolService(plugin)
    wrapper = ContextWrapper(context=context.context, tool_call_timeout=1)

    async def deliver(*args, **kwargs):
        await asyncio.Event().wait()

    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once", side_effect=deliver
    ) as sender:
        with pytest.raises(Exception, match="execution timeout"):
            _ = [
                item
                async for item in FunctionToolExecutor.execute(
                    GetPxImageTool(service), wrapper, count=1
                )
            ]
        repeated = await service.image(context, {})
    assert sender.call_count == 1
    assert repeated["duplicate_call"] and repeated["interrupted"]
    assert repeated["delivery"] == {"unknown": 1} and not repeated["running"]
    plugin.downloader.defer_cleanup.assert_called_once_with(paths[0])
    assert (await plugin.checkin_store.find_profile("10001")).coins == 100


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_started_settlement(tmp_path):
    plugin, context, _ = await setup_flow(tmp_path, items=1)
    started, release = asyncio.Event(), asyncio.Event()
    spend = plugin.checkin_store.spend_coins

    async def slow_spend(**kwargs):
        started.set()
        await release.wait()
        return await spend(**kwargs)

    plugin.checkin_store.spend_coins = slow_spend
    service = ImageToolService(plugin)
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ):
        task = asyncio.create_task(service.image(context, {}))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    result = context.context.event.get_extra(service.EVENT_KEY)
    assert result.settlement == "completed" and not service._calls
    assert (await plugin.checkin_store.find_profile("10001")).coins == 80


@pytest.mark.asyncio
async def test_current_umo_rules_and_count_limit_are_authoritative(tmp_path):
    from astrbot_plugin_get_px.group_safety import SessionSafetyService
    from astrbot_plugin_get_px.pixiv.filters import FiltersMixin

    plugin, context, _ = await setup_flow(tmp_path, items=1)
    service = ImageToolService(plugin)
    plugin.safety_service = SessionSafetyService(
        {
            "content_dedupe": {
                "safety_groups": [
                    {
                        "name": "A",
                        "sessions": ["bot-a:GroupMessage:1"],
                        "allow_r18": True,
                        "custom_terms": [],
                    }
                ]
            }
        }
    )
    plugin._content_safety_policy = lambda event: FiltersMixin._content_safety_policy(
        plugin, event
    )
    assert (await service.get_context(context, {}))["allow_r18"]
    context.context.event.unified_msg_origin = "bot-b:GroupMessage:1"
    assert not (await service.get_context(context, {}))["allow_r18"]
    plugin.config["max_count"] = 1
    with patch(
        "astrbot_plugin_get_px.pixiv.search.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ):
        summary = await service.image(context, {"count": 5})
    assert summary["requested_count"] == 5 and summary["effective_count"] == 1
