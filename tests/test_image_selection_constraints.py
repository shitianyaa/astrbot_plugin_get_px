"""请求级筛选、实际文件尺寸与既有安全／结算边界。"""

import sys
from pathlib import Path
from types import MethodType
from unittest.mock import AsyncMock, patch

from PIL import Image
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus
from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.selection import ImageSelection
from astrbot_plugin_get_px.tools.service import ImageToolService
from test_image_tools import Event, setup_flow
from test_p_coin_cost import _illust


@pytest.mark.parametrize(
    "args,size,expected",
    [
        ({}, (None, 0), True),
        ({"orientation": "landscape"}, (1200, 800), True),
        ({"orientation": "landscape"}, (800, 800), False),
        ({"orientation": "portrait"}, (800, 1200), True),
        ({"orientation": "portrait"}, (1200, 800), False),
        ({"orientation": "square"}, (800, 800), True),
        ({"orientation": "square"}, (801, 800), False),
        ({"aspect_ratio": 0.75, "ratio_tolerance": 0.15}, (600, 1000), True),
        ({"aspect_ratio": 0.75, "ratio_tolerance": 0.15}, (900, 1000), True),
        ({"aspect_ratio": 0.75, "ratio_tolerance": 0.15}, (901, 1000), False),
        ({"aspect_ratio": 0.75, "ratio_tolerance": 0}, (750, 1000), True),
        ({"aspect_ratio": 0.75, "ratio_tolerance": 0}, (751, 1000), False),
        ({"orientation": "landscape", "aspect_ratio": 0.75}, (750, 1000), False),
        ({"min_width": 1000, "min_height": 1500}, (1000, 1500), True),
        ({"min_width": 1000, "min_height": 1500}, (999, 1500), False),
        ({"min_width": 1000, "min_height": 1500}, (1000, 1499), False),
        ({"orientation": "portrait"}, (None, 1500), False),
        ({"orientation": "portrait"}, (True, 1500), False),
        ({"orientation": "portrait"}, (0, 1500), False),
    ],
)
def test_dimension_constraints(args, size, expected):
    assert ImageSelection.from_args(args).matches_dimensions(*size) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("admin,requested,effective", [(10, 3, 3), (2, 3, 2), (0, 3, 3), (0, 1e308, 50), (10, None, 10), (0, None, 0)])
async def test_tool_size_limit_can_only_tighten_and_does_not_write_config(
    tmp_path, admin, requested, effective
):
    plugin, context, _ = await setup_flow(tmp_path, items=1)
    plugin.config["auto_downgrade_original_mb"] = admin
    before = dict(plugin.config)
    args = {} if requested is None else {"max_size_mib": requested}
    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)):
        summary = await ImageToolService(plugin).image(context, args)
    assert summary["delivery"] == {"confirmed": 1}
    assert plugin.downloader.download_for_send.await_args.kwargs["size_limit_bytes"] == effective * 1024 * 1024
    assert plugin.config == before
    assert not plugin.downloader.download_for_send.await_args.kwargs["allow_quality_fallback"]


@pytest.mark.asyncio
@pytest.mark.parametrize("file_size", [(60, 80), (160, 120), None])
async def test_metadata_filter_precedes_claim_and_real_file_filter_releases_usage(tmp_path, file_size):
    plugin, context, paths = await setup_flow(tmp_path, items=3)
    plugin.config.update(dedupe_days=1, image_quality="large")
    plugin.image_index = ImageIndexStore(tmp_path, retention_days=1)
    plugin._pick_illusts = MethodType(FiltersMixin._pick_illusts, plugin)
    # 1元信息为横图；2原作够大但所选large文件太小；3满足全部条件。
    plugin.illusts[0].update(width=200, height=100)
    for item in plugin.illusts[1:]:
        item.update(width=120, height=160)
    if file_size is not None:
        Image.new("RGB", file_size).save(paths[1])
    Image.new("RGB", (120, 160)).save(paths[2])
    plugin.downloader.download_for_send.side_effect = [(paths[1], "large", 5), (paths[2], "large", 5)]
    plugin._fetch_source_candidates = AsyncMock(wraps=plugin._fetch_source_candidates)
    plugin.image_index.claim_usage = AsyncMock(wraps=plugin.image_index.claim_usage)
    args = {"orientation": "portrait", "aspect_ratio": 0.75, "ratio_tolerance": 0, "min_width": 100, "min_height": 150}
    try:
        with patch("astrbot_plugin_get_px.pixiv.filters.random.sample", side_effect=lambda seq, n: list(seq)[:n]), patch(
            "astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)
        ) as sender:
            summary = await ImageToolService(plugin).image(context, args)
        assert summary["downloaded_count"] == 1 and summary["delivery"] == {"confirmed": 1}
        assert [call.args[0]["id"] for call in plugin.downloader.download_for_send.await_args_list] == ["2", "3"]
        plugin._fetch_source_candidates.assert_awaited_once()
        assert [call.kwargs["illust_id"] for call in plugin.image_index.claim_usage.await_args_list] == ["2", "3"]
        assert sender.await_count == 1
        assert (await plugin.checkin_store.find_profile("10001")).coins == 80
        rows = plugin.image_index._get_conn().execute("SELECT illust_id, feature FROM image_usage").fetchall()
        assert [(row["illust_id"], row["feature"]) for row in rows] == [("3", "normal")]
        assert Path(paths[0]).exists() and not Path(paths[1]).exists() and not Path(paths[2]).exists()
    finally:
        plugin.image_index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [False, True])
async def test_filtered_batches_replenish_without_relaxing_or_repeating(tmp_path, available):
    plugin, context, _ = await setup_flow(tmp_path, items=1)
    wrong = dict(_illust(10), width=160, height=120)
    good = dict(_illust(11), width=120, height=160)
    plugin._fetch_source_candidates = AsyncMock(side_effect=[
        ([wrong], 1, "pixiv:search:test"),
        ([good] if available else [wrong], 1, "lolicon:search:test"),
        ([wrong], 1, "lolicon:search:test"),
    ])
    path = tmp_path / "portrait.png"
    Image.new("RGB", (120, 160)).save(path)
    plugin.downloader.download_for_send.return_value = (str(path), "original", path.stat().st_size)
    plugin.downloader.download_for_send.side_effect = None
    service = ImageToolService(plugin)
    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock(return_value=DeliveryStatus.CONFIRMED)) as sender:
        summary = await service.image(context, {"tag": "test", "count": 2, "orientation": "portrait"})
        repeated = await service.image(context, {"orientation": "landscape"})
    assert repeated["duplicate_call"]
    assert plugin._fetch_source_candidates.await_count == 3
    assert plugin.downloader.download_for_send.await_count == int(available)
    assert sender.await_count == int(available)
    assert summary["downloaded_count"] == int(available) and summary["message"]
    assert (await plugin.checkin_store.find_profile("10001")).coins == 100 - 20 * int(available)


@pytest.mark.asyncio
async def test_valid_dimensions_never_override_content_safety_or_service_failure(tmp_path):
    plugin, context, paths = await setup_flow(tmp_path, items=1)
    plugin.illusts[0].update(width=120, height=160, x_restrict=1)
    Image.new("RGB", (120, 160)).save(paths[0])
    plugin._filter_blacklisted_illusts = MethodType(FiltersMixin._filter_blacklisted_illusts, plugin)
    service = ImageToolService(plugin)
    with patch("astrbot_plugin_get_px.pixiv.execution.send_once", AsyncMock()) as sender:
        summary = await service.image(context, {"orientation": "portrait"})
        assert not summary["downloaded_count"]
        context.context.event = Event()
        plugin._filter_blacklisted_illusts = AsyncMock(side_effect=RuntimeError("fixture"))
        summary = await service.image(context, {"orientation": "portrait"})
        assert "服务" in summary["message"]
    sender.assert_not_awaited()
    plugin.downloader.download_for_send.assert_not_awaited()
    assert (await plugin.checkin_store.find_profile("10001")).coins == 100


@pytest.mark.parametrize("ratio,tolerance", [(0.1, 0.05), (0.05, 0.025), (0.75, 0.1)])
def test_default_ratio_tolerance_accepts_narrow_ratios(ratio, tolerance):
    selection = ImageSelection.from_args({"aspect_ratio": ratio})
    assert selection.ratio_tolerance == tolerance
    assert selection.matches_dimensions(50, int(50 / ratio))
    assert not selection.matches_dimensions(100, 100)
    assert ImageSelection.from_args({"aspect_ratio": ratio, "ratio_tolerance": 0}).ratio_tolerance == 0
