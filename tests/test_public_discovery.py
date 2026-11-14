"""公开查询、真实续页与明确作品发送的领域边界。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_image_tools import Event, setup_flow
from test_p_coin_cost import _illust
from astrbot_plugin_get_px.pixiv.client import PixivPage
from astrbot_plugin_get_px.pixiv.discovery import CURSOR_LIMIT
from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.query import QueryError
from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus
from astrbot_plugin_get_px.tools.service import ImageToolService


async def flow(tmp_path, items=10):
    plugin, context, paths = await setup_flow(tmp_path, items=items)
    for name in (
        "_safety_terms",
        "_blacklisted_illust_ids",
        "_blocked_query_term",
        "_filter_blacklisted_illusts",
        "_pick_illusts",
        "_matched_safety_term",
        "_illust_blacklist_ids",
    ):
        if name in ("_matched_safety_term", "_illust_blacklist_ids"):
            setattr(plugin, name, getattr(FiltersMixin, name))
        else:
            setattr(plugin, name, getattr(FiltersMixin, name).__get__(plugin))
    plugin._filter_manga = FiltersMixin._filter_manga
    plugin.config["pixiv_refresh_token"] = "fixture-token"
    plugin.client = SimpleNamespace(
        query_page=AsyncMock(
            return_value=PixivPage([_illust(i + 1) for i in range(items)])
        ),
        public_detail=AsyncMock(side_effect=lambda identity: _illust(identity)),
        users_page=AsyncMock(
            return_value=PixivPage(
                [
                    {
                        "user": {
                            "id": 12,
                            "name": "artist",
                            "account": "public",
                            "is_followed": True,
                        }
                    }
                ]
            )
        ),
        public_user=AsyncMock(
            return_value={
                "id": 12,
                "name": "artist",
                "account": "public",
                "is_followed": True,
            }
        ),
    )
    return plugin, context, paths, ImageToolService(plugin)


def next_cursor(result):
    return result["pagination"]["next_cursor"]


@pytest.mark.asyncio
async def test_queries_preserve_buffer_and_do_not_send_charge_or_claim(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.image_index = ImageIndexStore(tmp_path)
    plugin.client.query_page.side_effect = [
        PixivPage([_illust(i) for i in range(1, 7)], {"offset": 6}),
        PixivPage([_illust(i) for i in range(6, 11)]),
    ]
    before = (await plugin.checkin_store.find_profile("10001")).coins
    first = await service.query(context, "artworks", {"mode": "ranking", "limit": 3})
    assert [r["id"] for r in first["records"]] == ["1", "2", "3"]
    second = await service.query(context, "artworks", {"cursor": next_cursor(first)})
    assert [r["id"] for r in second["records"]] == ["4", "5", "6"]
    repeated = await service.query(context, "artworks", {"cursor": next_cursor(first)})
    assert repeated == second
    third = await service.query(context, "artworks", {"cursor": next_cursor(second)})
    assert [r["id"] for r in third["records"]] == ["7", "8", "9"]
    fourth = await service.query(context, "artworks", {"cursor": next_cursor(third)})
    assert [r["id"] for r in fourth["records"]] == ["10"]
    assert not fourth["pagination"]["has_more"]
    assert plugin.client.query_page.await_count == 2
    assert (
        not context.context.event.sent
        and not plugin.downloader.download_for_send.called
    )
    assert (await plugin.checkin_store.find_profile("10001")).coins == before
    assert not await plugin.image_index.get_used_illust_ids(
        "group:group:1", "pixiv:ranking"
    )
    assert (
        plugin.image_index._get_conn()
        .execute("SELECT COUNT(*) FROM image_usage")
        .fetchone()[0]
        == 0
    )
    assert (
        plugin.image_index._get_conn()
        .execute("SELECT COUNT(*) FROM source_page_cursor")
        .fetchone()[0]
        == 0
    )
    assert context.context.event.get_extra(service.EVENT_KEY) is None
    plugin.image_index.close()


@pytest.mark.asyncio
async def test_cursor_uses_real_upstream_position_and_filter_scan_budget(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.client.query_page.side_effect = [
        PixivPage([dict(_illust(1), x_restrict=1)], {"offset": 30}),
        PixivPage([dict(_illust(2), x_restrict=1)], {"offset": 60}),
        PixivPage([dict(_illust(3), x_restrict=1)], {"offset": 90}),
        PixivPage([_illust(4)]),
    ]
    result = await service.query(context, "artworks", {"mode": "ranking"})
    assert result["status"] == "filtered" and result["pagination"]["has_more"]
    assert plugin.client.query_page.await_count == 3
    result = await service.query(context, "artworks", {"cursor": next_cursor(result)})
    assert result["records"][0]["id"] == "4"
    assert plugin.client.query_page.await_args.args[1] == {"offset": 90}


@pytest.mark.asyncio
async def test_cursor_rechecks_context_policy_account_expiry_and_kind(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.config["rate_limit_seconds"] = 0
    result = await service.query(context, "artworks", {"mode": "ranking", "limit": 2})
    token = next_cursor(result)
    mixed = await service.query(context, "artworks", {"cursor": token, "limit": 1})
    assert mixed["status"] == "invalid_request"
    other = SimpleNamespace(
        context=SimpleNamespace(event=Event()), tool_call_timeout=120
    )
    other.context.event.unified_msg_origin = "bot-a:GroupMessage:2"
    assert (await service.query(other, "artworks", {"cursor": token}))[
        "status"
    ] == "cursor_invalid"
    assert (await service.query(context, "users", {"cursor": token}))[
        "status"
    ] == "cursor_invalid"
    plugin.config["pixiv_refresh_token"] = "new-fixture-token"
    assert (await service.query(context, "artworks", {"cursor": token}))[
        "status"
    ] == "cursor_invalid"
    plugin.config["pixiv_refresh_token"] = "fixture-token"
    context.context.event = Event()
    service.discovery.cursors[token].expires = 0
    assert (await service.query(context, "artworks", {"cursor": token}))[
        "status"
    ] == "cursor_invalid"


@pytest.mark.asyncio
async def test_cursor_memory_is_bounded_and_unload_clears_all_tools(tmp_path, monkeypatch):
    _, context, _, service = await flow(tmp_path)
    service.register()
    assert {tool.name for tool in service.tools} == {
        "get_px_image",
        "get_px_context",
        "get_px_artworks",
        "get_px_users",
    }
    first = await service.query(context, "artworks", {"mode": "ranking", "limit": 2})
    monkeypatch.setattr("astrbot_plugin_get_px.pixiv.discovery.QUERY_RECORD_LIMIT", 3)
    capped = await service.query(context, "artworks", {"cursor": next_cursor(first)})
    assert [r["id"] for r in capped["records"]] == ["3"]
    assert capped["pagination"] == {"returned": 1, "has_more": False, "limit_reached": True}
    template = next(iter(service.discovery.cursors.values()))
    for _ in range(CURSOR_LIMIT + 1):
        service.discovery._store(replace(template))
    assert len(service.discovery.cursors) == CURSOR_LIMIT
    await service.close()
    assert (
        not service.discovery.cursors
        and not service.plugin.context.get_llm_tool_manager().func_list
    )
    assert all(not tool.active for tool in service.tools)


@pytest.mark.asyncio
async def test_free_no_token_sampling_and_pixiv_modes_do_not_fall_back(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.config.update(p_coin_cost=0, pixiv_refresh_token="")
    plugin.checkin_store = None
    plugin.client = None
    plugin._init_client = lambda: None
    plugin.lolicon_client = SimpleNamespace(
        available=True, random=AsyncMock(return_value=[_illust(1)])
    )
    result = await service.query(context, "artworks", {})
    assert result["sampling"] == "random" and not result["pagination"]["has_more"]
    result = await service.query(context, "artworks", {"mode": "ranking"})
    assert result["status"] == "authentication_required"
    assert plugin.lolicon_client.random.await_count == 1
    result = await service.get_context(context, {})
    assert result["capabilities"]["available_modes"] == ["auto"]


@pytest.mark.asyncio
async def test_author_lookup_can_chain_into_ordered_send_without_query_side_effects(
    tmp_path,
):
    plugin, context, _, service = await flow(tmp_path, items=3)
    users = await service.query(context, "users", {"word": "artist"})
    assert users["records"][0]["id"] == "12"
    assert "is_followed" not in json.dumps(users)
    with patch(
        "astrbot_plugin_get_px.pixiv.execution.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ):
        summary = await service.image(
            context, {"mode": "author", "user_id": "12", "count": 3}
        )
    assert summary["downloaded_count"] == 3 and summary["settlement"] == "completed"
    assert [
        call.args[0]["id"]
        for call in plugin.downloader.download_for_send.await_args_list
    ] == ["1", "2", "3"]
    assert (await plugin.checkin_store.find_profile("10001")).coins == 40


@pytest.mark.asyncio
async def test_explicit_pages_deduplicate_targets_keep_order_and_report_skips(tmp_path):
    plugin, context, _, service = await flow(tmp_path, items=4)
    original = dict(
        _illust(42),
        page_count=2,
        meta_pages=[
            {"image_urls": {"original": "https://i.pximg.net/first.jpg"}},
            {"image_urls": {"original": "https://i.pximg.net/second.jpg"}},
        ],
    )
    plugin.client.public_detail = AsyncMock(return_value=original)
    targets = [
        {"artwork": "42", "page": 2},
        {"artwork": "https://www.pixiv.net/artworks/42", "page": 2},
        {"artwork": "42"},
        {"artwork": "42", "page": 3},
    ]
    with patch(
        "astrbot_plugin_get_px.pixiv.execution.send_once",
        AsyncMock(return_value=DeliveryStatus.ACCEPTED),
    ):
        result = await service.image(context, {"mode": "artwork", "targets": targets})
        again = await service.image(context, {"mode": "artwork", "targets": targets})
    assert result["requested_count"] == 3 and result["downloaded_count"] == 2
    assert [item["status"] for item in result["targets"]] == [
        "accepted",
        "accepted",
        "page_unavailable",
    ]
    assert [
        call.args[0]["id"]
        for call in plugin.downloader.download_for_send.await_args_list
    ] == ["42:1", "42"]
    assert (
        plugin.downloader.download_for_send.await_args_list[0]
        .args[0]["image_urls"]["original"]
        .endswith("second.jpg")
    )
    assert plugin.client.public_detail.await_count == 1 and again["duplicate_call"]


@pytest.mark.asyncio
async def test_explicit_repeat_allows_completed_history_but_not_pending_claim(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.image_index = ImageIndexStore(tmp_path)
    plugin._dedupe_days_cfg = 1
    await plugin.image_index.record_usage(
        scope=plugin._event_scope(context.context.event),
        source_key="pixiv:artwork",
        illust_id="1",
        feature="normal",
    )
    with patch(
        "astrbot_plugin_get_px.pixiv.execution.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ):
        result = await service.image(
            context, {"mode": "artwork", "targets": [{"artwork": "1"}]}
        )
    assert result["delivery"] == {"confirmed": 1}
    scope = plugin._event_scope(context.context.event)
    assert await plugin.image_index.claim_usage(
        scope=scope,
        source_key="pixiv:artwork:explicit_pending",
        illust_id="1",
        feature="normal_pending",
    )
    assert not await plugin.image_index.claim_usage(
        scope=scope,
        source_key="pixiv:artwork:explicit_pending",
        illust_id="1",
        feature="normal_pending",
    )
    plugin.image_index.close()


@pytest.mark.asyncio
async def test_related_seed_and_output_are_filtered_before_exposure(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.client.public_detail.return_value = dict(_illust(1), x_restrict=1)
    plugin.client.public_detail.side_effect = None
    result = await service.query(
        context, "artworks", {"mode": "related", "illust_id": "1"}
    )
    assert result["status"] == "filtered" and not result["records"]
    assert not plugin.client.query_page.called


@pytest.mark.asyncio
async def test_query_budget_cancel_and_concurrent_repeat_have_no_send(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(*args):
        started.set()
        await release.wait()
        return PixivPage([_illust(1)])

    plugin.client.query_page.side_effect = slow
    first = asyncio.create_task(service.query(context, "artworks", {"mode": "ranking"}))
    await started.wait()
    second = asyncio.create_task(
        service.query(context, "artworks", {"mode": "ranking"})
    )
    release.set()
    assert await first == await second
    assert plugin.client.query_page.await_count == 1
    context.context.event = Event()
    plugin.config["rate_limit_seconds"] = 0
    context.tool_call_timeout = 0.01
    release.clear()
    result = await service.query(context, "artworks", {"mode": "ranking"})
    assert (
        result["status"] == "budget_exhausted"
        and not plugin.downloader.download_for_send.called
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [DeliveryStatus.UNKNOWN, DeliveryStatus.TIMED_OUT, DeliveryStatus.CONFIRMED],
)
async def test_explicit_delivery_settlement_and_pending_cleanup(tmp_path, status):
    plugin, context, _, service = await flow(tmp_path)
    plugin.image_index = ImageIndexStore(tmp_path)
    plugin.config["dedupe_days"] = 0
    with patch(
        "astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=status)
    ):
        result = await service.image(
            context, {"mode": "artwork", "targets": [{"artwork": "1"}]}
        )
    assert result["targets"][0]["status"] == status.value
    coins = (await plugin.checkin_store.find_profile("10001")).coins
    assert coins == (100 if status is DeliveryStatus.UNKNOWN else 80)
    pending = await plugin.image_index.get_used_illust_ids(
        plugin._event_scope(context.context.event), "pixiv:artwork:explicit_pending"
    )
    assert bool(pending) is (status is DeliveryStatus.UNKNOWN)
    if status is DeliveryStatus.UNKNOWN:
        context.context.event = Event()
        with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock()) as sender:
            blocked = await service.image(context, {"mode": "artwork", "targets": [{"artwork": "1"}]})
        sender.assert_not_awaited()
        assert blocked["downloaded_count"] == 0
    plugin.image_index.close()


@pytest.mark.asyncio
async def test_explicit_failure_preserves_completed_history_and_count_limits(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.image_index = ImageIndexStore(tmp_path)
    plugin._dedupe_days_cfg = 1
    plugin.config["max_count"] = 1
    scope = plugin._event_scope(context.context.event)
    await plugin.image_index.record_usage(
        scope=scope, source_key="pixiv:artwork", illust_id="1", feature="normal"
    )
    plugin.downloader.download_for_send.side_effect = RuntimeError("download failed")
    result = await service.image(
        context, {"mode": "artwork", "targets": [{"artwork": "1"}, {"artwork": "2"}]}
    )
    assert [item["status"] for item in result["targets"]] == [
        "download_failed",
        "count_limited",
    ]
    assert result["count_limits"] == ["max_count"] and result["downloaded_count"] == 0
    assert not await plugin.image_index.get_used_illust_ids(
        scope, "pixiv:artwork:explicit_pending"
    )
    assert await plugin.image_index.get_used_illust_ids(scope, "pixiv:artwork") == {"1"}
    assert plugin.client.public_detail.await_count == 1
    plugin.image_index.close()


@pytest.mark.asyncio
async def test_explicit_concurrent_events_and_cancel_keep_first_claim(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(identity):
        started.set()
        await release.wait()
        return _illust(identity)

    plugin.client.public_detail.side_effect = slow
    args = {"mode": "artwork", "targets": [{"artwork": "1"}]}
    first = asyncio.create_task(service.image(context, args))
    await started.wait()
    other = SimpleNamespace(
        context=SimpleNamespace(event=Event()), tool_call_timeout=120
    )
    result = await service.image(other, args)
    assert not result["running"] and not result["delivery"]
    assert service._explicit_claims
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not service._explicit_claims and not service._calls
    assert not plugin.downloader.download_for_send.called


@pytest.mark.asyncio
async def test_real_sdk_executor_exposes_query_tools_as_text(tmp_path):
    from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
    from astrbot.core.agent.run_context import ContextWrapper

    plugin, context, _, service = await flow(tmp_path)
    service.register()
    for tool in service.tools:
        json.dumps(tool.parameters)
    tool = next(tool for tool in service.tools if tool.name == "get_px_users")
    outputs = [
        item
        async for item in FunctionToolExecutor.execute(
            tool,
            ContextWrapper(context=context.context, tool_call_timeout=120),
            user_id="12",
        )
    ]
    assert json.loads(outputs[0].content[0].text)["records"][0]["id"] == "12"
    assert all(item.type == "text" for item in outputs[0].content)
    assert not plugin.downloader.download_for_send.called
    await service.close()


@pytest.mark.asyncio
async def test_cursor_expiry_and_actual_rules_are_checked_even_for_event_memo(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    first = await service.query(context, "artworks", {"mode": "ranking", "limit": 1})
    token = next_cursor(first)
    await service.query(context, "artworks", {"cursor": token})
    service.discovery.cursors[token].expires = 0
    assert (await service.query(context, "artworks", {"cursor": token}))[
        "status"
    ] == "cursor_invalid"
    fresh = await service.query(context, "artworks", {"mode": "ranking", "limit": 2})
    plugin.image_index = ImageIndexStore(tmp_path)
    await plugin.image_index.add_safety_term("new-block")
    assert (await service.query(context, "artworks", {"cursor": next_cursor(fresh)}))[
        "status"
    ] == "cursor_invalid"
    plugin.image_index.close()


@pytest.mark.asyncio
async def test_new_mode_keeps_upstream_failure_distinct_from_empty(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.client.query_page.side_effect = QueryError(
        "upstream_error", "公开查询失败。"
    )
    result = await service.query(context, "artworks", {"mode": "search", "tag": "test"})
    assert result["status"] == "upstream_error"
    plugin.client.query_page.side_effect = None
    plugin.client.query_page.return_value = PixivPage([])
    result = await service.query(
        context, "artworks", {"mode": "author", "user_id": "12"}
    )
    assert result["status"] == "empty" and not result["pagination"]["has_more"]


@pytest.mark.asyncio
async def test_query_unload_cancels_all_inflight_readers_and_clears_state(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    service.register()
    started = asyncio.Event()

    async def hang(*args):
        started.set()
        await asyncio.Event().wait()

    plugin.client.query_page.side_effect = hang
    first = asyncio.create_task(service.query(context, "artworks", {"mode": "ranking"}))
    await started.wait()
    duplicate = asyncio.create_task(
        service.query(context, "artworks", {"mode": "ranking"})
    )
    await asyncio.sleep(0)
    await service.close()
    assert first.cancelled() and duplicate.cancelled()
    assert not service._calls and not service.discovery.cursors
    assert not plugin.downloader.download_for_send.called


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "private_field", [{"visible": False}, {"restrict": 1}, {"type": "ugoira"}]
)
async def test_new_query_and_explicit_send_skip_nonpublic_or_animated_work(
    tmp_path, private_field
):
    plugin, context, _, service = await flow(tmp_path)
    private = dict(_illust(1), **private_field)
    plugin.client.public_detail.side_effect = None
    plugin.client.public_detail.return_value = private
    plugin.client.query_page.return_value = PixivPage([private])
    result = await service.query(context, "artworks", {"mode": "ranking"})
    assert result["status"] == "filtered" and not result["records"]
    result = await service.image(
        context, {"mode": "artwork", "targets": [{"artwork": "1"}]}
    )
    assert result["targets"][0]["status"] == "filtered" and not result["delivery"]
    assert not plugin.downloader.download_for_send.called


@pytest.mark.asyncio
async def test_related_seed_is_not_subject_to_result_dimensions(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.client.public_detail.side_effect = None
    plugin.client.public_detail.return_value = dict(_illust(1), width=600, height=1000)
    plugin.client.query_page.return_value = PixivPage(
        [dict(_illust(2), width=1600, height=900)]
    )
    result = await service.query(
        context,
        "artworks",
        {"mode": "related", "illust_id": "1", "orientation": "landscape"},
    )
    assert result["status"] == "ok" and result["records"][0]["id"] == "2"


@pytest.mark.asyncio
async def test_lolicon_candidate_preserves_page_without_inventing_total_pages(tmp_path):
    from astrbot_plugin_get_px.pixiv.lolicon import LoliconClient

    plugin, context, _, service = await flow(tmp_path)
    plugin.config.update(p_coin_cost=0, pixiv_refresh_token="")
    plugin.client, plugin.checkin_store = None, None
    plugin._init_client = lambda: None
    candidate = LoliconClient._normalize(
        {"pid": 42, "p": 3, "uid": 12, "title": "public", "urls": {}}
    )
    plugin.lolicon_client = SimpleNamespace(
        available=True, random=AsyncMock(return_value=[candidate])
    )
    result = await service.query(context, "artworks", {})
    record = result["records"][0]
    assert record["page"] == 4 and "page_count" not in record


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "file_dimensions, expected", [((40, 20), "confirmed"), ((10, 30), "filtered")]
)
async def test_explicit_nonfirst_page_checks_actual_file_not_first_page_dimensions(
    tmp_path,
    file_dimensions,
    expected,
):
    from PIL import Image

    plugin, context, paths, service = await flow(tmp_path)
    original = dict(
        _illust(42),
        width=10,
        height=30,
        page_count=2,
        meta_pages=[
            {"image_urls": {"original": "https://i.pximg.net/first.jpg"}},
            {"image_urls": {"original": "https://i.pximg.net/second.jpg"}},
        ],
    )
    plugin.client.public_detail.side_effect = None
    plugin.client.public_detail.return_value = original
    targets = [{"artwork": "42", "page": 2}]
    record = (
        await service.query(
            context, "artworks", {"mode": "artwork", "targets": targets}
        )
    )["records"][0]
    assert record["width"] is None and record["height"] is None
    filtered_query = await service.query(
        context,
        "artworks",
        {"mode": "artwork", "targets": targets, "orientation": "landscape"},
    )
    assert filtered_query["status"] == "filtered"
    assert filtered_query["targets"][0]["status"] == "filtered"
    Image.new("RGB", file_dimensions).save(paths[0])
    with patch(
        "astrbot_plugin_get_px.pixiv.execution.send_once",
        AsyncMock(return_value=DeliveryStatus.CONFIRMED),
    ):
        result = await service.image(
            context,
            {
                "mode": "artwork",
                "targets": targets,
                "orientation": "landscape",
                "min_width": 40,
            },
        )
    assert result["delivery"] == ({"confirmed": 1} if expected == "confirmed" else {})
    assert result["targets"][0]["status"] == expected


@pytest.mark.asyncio
async def test_explicit_query_filtering_is_not_reported_as_missing_source(tmp_path):
    plugin, context, _, service = await flow(tmp_path)
    plugin.client.public_detail.side_effect = None
    plugin.client.public_detail.return_value = dict(_illust(1), x_restrict=1)
    result = await service.query(
        context, "artworks", {"mode": "artwork", "targets": [{"artwork": "1"}]}
    )
    assert result["status"] == "filtered"
    assert result["targets"][0]["status"] == "filtered"
