"""Tests for the /p command coin charging."""

import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin import (
    CheckinProfile,
    CheckinStore,
    CoinSpendResult,
)
from astrbot_plugin_get_px.pixiv.search import SearchMixin


@pytest.mark.asyncio
async def test_spend_coins_deducts_and_returns_new_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        # 先创建成员并充值到足够余额
        await store.get_profile("10001")
        await store.update_checkin_member(
            user_id="10001", coins=100, affection=0.0, total_days=0, streak_days=0
        )
        result = await store.spend_coins(user_id="10001", cost=30)
        assert isinstance(result, CoinSpendResult)
        assert result.success is True
        assert result.profile.coins == 70
        profile = await store.get_profile("10001")
        assert profile.coins == 70


@pytest.mark.asyncio
async def test_spend_coins_success_does_not_reread_profile_after_commit() -> None:
    """扣费提交后不得回读档案：回读失败不得把已扣费误报为异常/未扣费。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        await store.get_profile("10001")
        await store.update_checkin_member(
            user_id="10001", coins=100, affection=0.0, total_days=0, streak_days=0
        )
        with patch.object(
            store,
            "_get_or_create_profile_sync",
            side_effect=RuntimeError("提交后回读不应发生"),
        ):
            result = await store.spend_coins(user_id="10001", cost=30)
        assert result.success is True
        assert result.profile.coins == 70
        profile = await store.get_profile("10001")
        assert profile.coins == 70


@pytest.mark.asyncio
async def test_spend_coins_insufficient_leaves_balance_untouched() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        await store.get_profile("10001")
        await store.update_checkin_member(
            user_id="10001", coins=10, affection=0.0, total_days=0, streak_days=0
        )
        result = await store.spend_coins(user_id="10001", cost=30)
        assert result.success is False
        assert "金币不足，需要 30，当前只有 10。" in result.message
        profile = await store.get_profile("10001")
        assert profile.coins == 10


class _MinSearch(SearchMixin):
    def __init__(self):
        self.config = {}
        self.checkin_store = None
        self._p_charge_user_id = ""

    def _cfg_int(self, key, default, lo, hi):
        val = self.config.get(key, default)
        return int(val) if lo <= int(val) <= hi else default

    def _cfg_bool(self, key, default):
        return bool(self.config.get(key, default))


def test_p_unit_cost_defaults_to_20_and_zero_disables() -> None:
    mixin = _MinSearch()
    assert mixin._p_unit_cost() == 20
    mixin.config["p_coin_cost"] = 0
    assert mixin._p_unit_cost() == 0
    assert mixin._p_charging_active() is False


def test_p_charging_active_requires_checkin_store() -> None:
    mixin = _MinSearch()
    assert mixin._p_charging_active() is False  # checkin_store 为 None


@pytest.mark.asyncio
async def test_p_balance_error_reports_shortfall() -> None:
    mixin = _MinSearch()
    mixin.checkin_store = AsyncMock()
    profile = AsyncMock()
    profile.coins = 25
    mixin.checkin_store.get_profile = AsyncMock(return_value=profile)
    error = await mixin._p_balance_error("10001", 3)
    assert "金币不足，需要 60，当前只有 25。" in error
    ok = await mixin._p_balance_error("10001", 1)
    assert ok == ""


@pytest.mark.asyncio
async def test_spend_coins_unknown_user_creates_profile_and_declines_quickly() -> None:
    """新用户分支必须在同一连接上建档案，不能触发写锁死锁。"""
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        result = await store.spend_coins(user_id="90001", cost=30)
        assert result.success is False
        assert "金币不足，需要 30，当前只有 0。" in result.message
        profile = await store.get_profile("90001")
        assert profile.coins == 0


class _SearchFlowHarness(SearchMixin):
    """轻量集成桩：驱动 _handle_search 走到结算，只打桩网络与事件。"""

    def __init__(self, platform="qq_official", illusts=None):
        self.config = {"p_coin_cost": 20, "checkin_enabled": True}
        self.platform = platform
        self.illusts = illusts or []
        self.image_index = None
        self.checkin_store = None
        self.downloader = None
        self._dedupe_days_cfg = 0
        self.download_results = []
        self.sent_chains = []

    def _cfg_int(self, key, default, lo, hi):
        if key == "dedupe_days":
            return self._dedupe_days_cfg
        return int(self.config.get(key, default))

    def _cfg_float(self, key, default, lo, hi):
        return float(self.config.get(key, default))

    def _cfg_str(self, key, default=""):
        return str(self.config.get(key, default))

    def _cfg_bool(self, key, default):
        return bool(self.config.get(key, default))

    def _check_rate_limit(self, user_id):
        return 0

    def _forward_threshold(self):
        return 0

    async def _content_safety_policy(self, event):
        from astrbot_plugin_get_px.pixiv.safety import ContentSafetyPolicy
        return ContentSafetyPolicy()

    async def _blocked_query_term(self, query, policy=None):
        return ""

    def _filter_manga(self, illusts):
        return illusts

    async def _filter_blacklisted_illusts(self, illusts, policy=None):
        return illusts

    async def _pick_illusts(
        self, event, illusts, pick_count, *, source_key, dedupe_enabled, raw_count
    ):
        return illusts[:pick_count]

    async def _fetch_source_candidates(
        self, event, tag, *, count=20, offset=0, aspect_ratio="", use_page_cursor=True,
        policy=None,
    ):
        return self.illusts, len(self.illusts), "lolicon:random"


class _FlowEvent:
    def __init__(self, platform="qq_official"):
        self.platform = platform
        self.sent = []

    def get_sender_id(self):
        return "10001"

    def get_group_id(self):
        return "group:1"

    def get_platform_name(self):
        return self.platform

    def get_self_id(self):
        return "self-bot"

    def plain_result(self, text):
        return f"plain:{text}"

    def chain_result(self, chain):
        return list(chain)

    async def send(self, chain):
        self.sent.append(chain)
        return None


def _settle_store(fail_settle: bool = False):
    store = AsyncMock()
    collect = {"coins": 1000}

    def _profile():
        return CheckinProfile(
            user_id="10001",
            coins=collect["coins"],
            affection=0.0,
            total_days=10,
            streak_days=2,
            last_checkin_date="2026-08-28",
            boost_start_date="",
            boost_until_date="",
            repeat_penalty_date="",
            repeat_penalty_total=0.0,
            created_at="2026-08-01T00:00:00+08:00",
            updated_at="2026-08-28T00:00:00+08:00",
        )

    async def spend(*args, **kwargs):
        if fail_settle:
            return CoinSpendResult(
                success=False,
                profile=_profile(),
                cost=int(kwargs["cost"]),
                message="金币不足，需要 40，当前只有 10。",
            )
        collect["coins"] -= int(kwargs["cost"])
        return CoinSpendResult(
            success=True, profile=_profile(), cost=int(kwargs["cost"]), message="扣费成功"
        )

    store.get_profile = AsyncMock(side_effect=lambda user: _profile())
    store.spend_coins = AsyncMock(side_effect=spend)
    return store, collect


def _sent_texts(event) -> list[str]:
    texts = []
    for sent in event.sent:
        if isinstance(sent, str):
            texts.append(sent)
        else:
            texts.extend(str(item) for item in sent)
    return texts


def _illust(ident):
    return {"id": str(ident), "title": f"t{ident}", "x_restrict": 0, "tags": []}


async def _run_search(harness, event, count_str):
    outputs = []
    async for item in harness._handle_search(event, "", count_str):
        outputs.append(item)
    return outputs


@pytest.mark.asyncio
async def test_search_settlement_charges_exactly_sent_count() -> None:
    harness = _SearchFlowHarness(illusts=[_illust(1), _illust(2), _illust(3)])
    harness.checkin_store, balance = _settle_store()
    downloader = AsyncMock()
    downloader.download_for_send = AsyncMock(
        side_effect=[
            ("p1.jpg", "medium", 1024),
            RuntimeError("网络中断"),
            ("p3.jpg", "medium", 1024),
        ]
    )
    harness.downloader = downloader
    event = _FlowEvent()
    await _run_search(harness, event, "3")

    harness.checkin_store.spend_coins.assert_awaited_once()
    args, kwargs = harness.checkin_store.spend_coins.await_args
    assert kwargs["cost"] == 40  # 2 张成功 × 单价 20
    assert kwargs["user_id"] == "10001"
    assert balance["coins"] == 960  # 1000 - 40 真实递减
    # 结算成功后必须向用户发出扣费提醒
    texts = _sent_texts(event)
    assert any("消耗 40 金币" in text for text in texts)
    assert any("余额 960" in text for text in texts)


@pytest.mark.asyncio
async def test_search_settlement_warns_user_when_charge_fails() -> None:
    """TOCTOU：结算时余额不足（success=False）必须向用户发出警告且不扣钱。"""
    harness = _SearchFlowHarness(illusts=[_illust(1)])
    harness.checkin_store, balance = _settle_store(fail_settle=True)
    downloader = AsyncMock()
    downloader.download_for_send = AsyncMock(return_value=("p1.jpg", "medium", 1024))
    harness.downloader = downloader
    event = _FlowEvent()
    await _run_search(harness, event, "1")

    harness.checkin_store.spend_coins.assert_awaited_once()
    assert balance["coins"] == 1000  # 结算失败，余额未被扣减
    texts = _sent_texts(event)
    assert any("⚠️" in text and "金币结算未完成" in text for text in texts)


@pytest.mark.asyncio
async def test_search_settlement_skips_charge_when_nothing_sent() -> None:
    harness = _SearchFlowHarness(illusts=[_illust(1)])
    harness.checkin_store, _balance = _settle_store()
    downloader = AsyncMock()
    downloader.download_for_send = AsyncMock(side_effect=RuntimeError("网络中断"))
    harness.downloader = downloader
    event = _FlowEvent()
    await _run_search(harness, event, "1")

    harness.checkin_store.spend_coins.assert_not_awaited()


@pytest.mark.asyncio
async def test_search_forward_branch_settles_downloaded_count() -> None:
    """合并转发成功路径同样按实际发送数一次性结算。"""
    harness = _SearchFlowHarness(
        platform="aiocqhttp", illusts=[_illust(1), _illust(2)]
    )
    harness.checkin_store, _balance = _settle_store()
    downloader = AsyncMock()
    downloader.download_for_send = AsyncMock(
        return_value=("p.jpg", "medium", 1024)
    )
    harness.downloader = downloader
    event = _FlowEvent(platform="aiocqhttp")
    await _run_search(harness, event, "2")

    harness.checkin_store.spend_coins.assert_awaited_once()
    args, kwargs = harness.checkin_store.spend_coins.await_args
    assert kwargs["cost"] == 40
    assert event.sent[0][0].nodes[0].uin == "self-bot"


@pytest.mark.asyncio
@pytest.mark.parametrize("forward", [False, True])
async def test_uncertain_send_does_not_retry_fallback_or_charge(forward):
    from aiocqhttp.exceptions import ActionFailed
    harness = _SearchFlowHarness(illusts=[_illust(1), _illust(2)])
    harness.checkin_store, _ = _settle_store()
    harness._should_use_forward = lambda **kwargs: forward
    harness.downloader = AsyncMock()
    harness.downloader.download_for_send.return_value = ("unused.jpg", "original", 1024)
    event = _FlowEvent(platform="aiocqhttp")
    delivered = []
    async def send(chain):
        if isinstance(chain, list):
            delivered.append(chain)
            raise ActionFailed({"status": "failed", "retcode": 1200, "message": "ambiguous"})
        event.sent.append(chain)
    event.send = send
    await _run_search(harness, event, "2")
    assert len(delivered) == (1 if forward else 2)
    harness.checkin_store.spend_coins.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_usage_error_does_not_repeat_confirmed_delivery():
    harness = _SearchFlowHarness(illusts=[_illust(1)])
    harness.checkin_store, _ = _settle_store()
    harness.downloader = AsyncMock()
    harness.downloader.download_for_send.return_value = ("unused.jpg", "original", 1024)
    harness.image_index = AsyncMock()
    harness.image_index.retention_days = 1
    harness.image_index.record_usage.side_effect = RuntimeError("database unavailable")
    event = _FlowEvent()
    await _run_search(harness, event, "1")
    assert len([chain for chain in event.sent if isinstance(chain, list)]) == 1
    harness.checkin_store.spend_coins.assert_awaited_once()
    harness.image_index.record_usage.assert_awaited_once()


@pytest.mark.parametrize("save_fails", [True, False])
def test_retired_transport_config_is_removed_with_save_rollback(save_fails):
    import json
    from astrbot_plugin_get_px.main import GetPxPlugin
    class Config(dict):
        def save_config(self):
            if save_fails:
                raise OSError("disk unavailable")
    plugin = object.__new__(GetPxPlugin)
    group = {"onebot_image_transport": "base64"}
    plugin.config = Config({"pixiv_source": group, "onebot_image_transport": "astrbot"})
    plugin._cleanup_retired_transport_config()
    assert plugin.config["pixiv_source"] is group
    assert ("onebot_image_transport" in group) is save_fails
    assert ("onebot_image_transport" in plugin.config) is save_fails
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "onebot_image_transport" not in schema["pixiv_source"]["items"]


@pytest.mark.asyncio
@pytest.mark.parametrize("forward", [False, True])
@pytest.mark.parametrize("outcome", [
    "confirmed", "failed", "timed_out", "protocol_timeout", "unknown", "cancelled",
    "empty_receipt", "build_cancelled",
])
async def test_delivery_state_controls_charge_pending_and_file_lifetime(tmp_path, forward, outcome):
    import asyncio
    from PIL import Image
    from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    count = 2 if forward else 1
    paths = [tmp_path / f"get_px_fixture_{i}.png" for i in range(1, count + 1)]
    for path in paths:
        Image.new("RGB", (8, 8), "blue").save(path)
    harness = _SearchFlowHarness(illusts=[_illust(i) for i in range(1, count + 1)])
    harness._forward_threshold = lambda: 0 if forward else 1
    harness._dedupe_days_cfg = 1
    harness.checkin_store, _ = _settle_store()
    index = ImageIndexStore(tmp_path, retention_days=1)
    harness.image_index = index
    downloader = ImageDownloader(temp_dir=tmp_path)
    downloader.download_for_send = AsyncMock(
        side_effect=[(str(path), "original", path.stat().st_size) for path in paths]
    )
    harness.downloader = downloader
    event = _FlowEvent(platform="aiocqhttp")
    event.get_group_id = lambda: "456"
    event.bot = Mock(call_action=AsyncMock(return_value={"message_id": 123}))
    if outcome == "timed_out":
        event.bot.call_action.side_effect = TimeoutError()
    elif outcome == "protocol_timeout":
        from aiocqhttp.exceptions import ActionFailed
        event.bot.call_action.side_effect = ActionFailed({"retcode": 1200, "message": "Timeout: NTEvent sendMsg"})
    elif outcome == "unknown":
        event.bot.call_action.side_effect = RuntimeError("ambiguous failure")
    elif outcome == "cancelled":
        event.bot.call_action.side_effect = asyncio.CancelledError()
    elif outcome == "empty_receipt":
        event.bot.call_action.return_value = None
    scope = harness._event_scope(event)
    ids = {str(i) for i in range(1, count + 1)}
    try:
        for illust_id in ids:
            await index.claim_usage(
                scope=scope, source_key="lolicon:random", illust_id=illust_id,
                feature="normal_pending", user_id="10001",
            )
        if outcome in ("failed", "build_cancelled"):
            error = asyncio.CancelledError() if outcome == "build_cancelled" else ValueError("invalid payload")
            with patch("astrbot_plugin_get_px.pixiv.delivery._onebot_request", side_effect=error):
                if outcome == "failed":
                    await _run_search(harness, event, str(count))
                else:
                    with pytest.raises(asyncio.CancelledError):
                        await _run_search(harness, event, str(count))
            event.bot.call_action.assert_not_awaited()
            if outcome == "failed":
                assert not event.sent
        elif outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await _run_search(harness, event, str(count))
        else:
            await _run_search(harness, event, str(count))
        held = await index.get_used_illust_ids(scope, "lolicon:random")
        assert held == (set() if outcome in ("failed", "build_cancelled") else ids)
        uncertain = outcome in ("empty_receipt", "timed_out", "protocol_timeout", "unknown", "cancelled")
        assert all(path.exists() is uncertain for path in paths)
        assert len(downloader._cleanup_tasks) == (count if uncertain else 0)
        if outcome in ("confirmed", "empty_receipt", "timed_out", "protocol_timeout"):
            args, params = event.bot.call_action.await_args
            assert args == ("send_group_forward_msg" if forward else "send_group_msg",)
            segments = params["messages"][0]["data"]["content"] if forward else params["message"]
            assert segments[1]["data"]["file"] == paths[0].resolve().as_uri()
            harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=20 * count)
        else:
            harness.checkin_store.spend_coins.assert_not_awaited()
    finally:
        await downloader.close()
        index.close()


@pytest.mark.asyncio
async def test_mixed_single_batch_charges_success_and_timeout_but_not_build_failure(tmp_path):
    from PIL import Image
    from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    paths = {str(i): tmp_path / f"get_px_mixed_{i}.png" for i in (1, 2, 3)}
    for path in paths.values():
        Image.new("RGB", (8, 8), "blue").save(path)
    harness = _SearchFlowHarness(illusts=[_illust(i) for i in (1, 2, 3)])
    harness._should_use_forward = lambda **_kwargs: False
    harness._dedupe_days_cfg = 1
    harness.checkin_store, _ = _settle_store()
    index = ImageIndexStore(tmp_path, retention_days=1)
    harness.image_index = index
    downloader = ImageDownloader(temp_dir=tmp_path)

    async def download(illust, *_args, **_kwargs):
        path = paths[str(illust["id"])]
        size = path.stat().st_size
        if str(illust["id"]) == "3":
            path.unlink()  # 模拟并发清理导致构建前文件丢失。
        return str(path), "original", size

    downloader.download_for_send = download
    harness.downloader = downloader
    event = _FlowEvent(platform="aiocqhttp")
    event.get_group_id = lambda: "456"
    event.bot = Mock(call_action=AsyncMock(side_effect=[{"message_id": 123}, TimeoutError()]))
    scope = harness._event_scope(event)
    try:
        for illust_id in paths:
            await index.claim_usage(
                scope=scope, source_key="lolicon:random", illust_id=illust_id,
                feature="normal_pending", user_id="10001",
            )
        await _run_search(harness, event, "3")
        assert await index.get_used_illust_ids(scope, "lolicon:random") == {"1", "2"}
        rows = index._get_conn().execute("SELECT illust_id, feature FROM image_usage").fetchall()
        assert {row["illust_id"]: row["feature"] for row in rows} == {"1": "normal", "2": "normal"}
        assert [path.exists() for path in paths.values()] == [False, True, False]
        assert event.bot.call_action.await_count == 2
        harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=40)
    finally:
        await downloader.close()
        index.close()


@pytest.mark.asyncio
async def test_oversized_work_is_replaced_without_charge_or_pending_usage(tmp_path):
    from types import MethodType
    from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader
    from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    harness = _SearchFlowHarness(illusts=[_illust(i) for i in (1, 2, 3)])
    harness._dedupe_days_cfg = 1
    harness.checkin_store, _ = _settle_store()
    harness._pick_illusts = MethodType(FiltersMixin._pick_illusts, harness)
    paths = {str(i): tmp_path / f"get_px_{i}.jpg" for i in (1, 2, 3)}
    for item in harness.illusts:
        item["meta_single_page"] = {"original_image_url": f"https://img/{item['id']}.jpg"}
    index = ImageIndexStore(tmp_path, retention_days=1)
    harness.image_index = index
    downloader = ImageDownloader(temp_dir=tmp_path)

    async def download(url, timeout, size_limit_bytes=0):
        ident = url.rsplit("/", 1)[1].split(".")[0]
        path = paths[ident]
        if ident == "1":
            from astrbot_plugin_get_px.pixiv.downloader import ImageSizeLimitError
            raise ImageSizeLimitError("图片超过大小上限")
        path.write_bytes(b"fixture")
        return str(path), 1024

    downloader.download = AsyncMock(side_effect=download)
    harness.downloader = downloader
    event = _FlowEvent()
    try:
        with patch("astrbot_plugin_get_px.pixiv.filters.random.sample", side_effect=lambda seq, n: list(seq)[:n]):
            await _run_search(harness, event, "2")
        assert downloader.download.await_count == 3
        assert await index.get_used_illust_ids(harness._event_scope(event), "lolicon:random") == {"2", "3"}
        assert all(not path.exists() for path in paths.values())
        harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=40)
    finally:
        await downloader.close()
        index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_replenishment_tracks_source_and_releases_skipped_claims(tmp_path, cancelled):
    import asyncio
    from types import MethodType
    from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
    from astrbot_plugin_get_px.pixiv.index import ImageIndexStore

    harness = _SearchFlowHarness()
    harness._dedupe_days_cfg = 1
    harness.checkin_store, _ = _settle_store()
    harness._pick_illusts = MethodType(FiltersMixin._pick_illusts, harness)
    harness._fetch_source_candidates = AsyncMock(side_effect=[
        ([_illust(1)], 1, "lolicon:random"),
        ([_illust(1)], 1, "lolicon:random"),
        ([_illust(2)], 1, "pixiv:recommended"),
    ])
    harness.downloader = AsyncMock()
    harness.downloader.download_for_send.side_effect = [
        RuntimeError("图片超过大小上限"),
        asyncio.CancelledError() if cancelled else ("unused.jpg", "original", 1024),
    ]
    index = ImageIndexStore(tmp_path, retention_days=1)
    harness.image_index = index
    event = _FlowEvent()
    try:
        if cancelled:
            with pytest.raises(asyncio.CancelledError):
                await _run_search(harness, event, "1")
        else:
            await _run_search(harness, event, "1")
        assert harness._fetch_source_candidates.await_count == 3
        assert harness.downloader.download_for_send.await_count == 2
        scope = harness._event_scope(event)
        assert await index.get_used_illust_ids(scope, "lolicon:random") == set()
        assert await index.get_used_illust_ids(scope, "pixiv:recommended") == (set() if cancelled else {"2"})
        if cancelled:
            harness.checkin_store.spend_coins.assert_not_awaited()
        else:
            harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=20)
        assert all(call.kwargs["allow_quality_fallback"] is False for call in harness.downloader.download_for_send.await_args_list)
    finally:
        index.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_batch", [False, True])
async def test_candidate_replacement_stops_at_download_or_batch_budget(repeat_batch):
    harness = _SearchFlowHarness(illusts=[_illust(1), _illust(1)] if repeat_batch else [_illust(i) for i in range(1, 26)])
    harness.checkin_store, _ = _settle_store()
    harness._fetch_source_candidates = AsyncMock(return_value=(harness.illusts, len(harness.illusts), "lolicon:random"))
    harness.downloader = AsyncMock()
    harness.downloader.download_for_send.side_effect = RuntimeError("图片超过大小上限")
    outputs = await _run_search(harness, _FlowEvent(), "2" if repeat_batch else "1")
    assert harness.downloader.download_for_send.await_count == (1 if repeat_batch else 20)
    assert harness._fetch_source_candidates.await_count == (3 if repeat_batch else 1)
    harness.checkin_store.spend_coins.assert_not_awaited()
    assert outputs and "失败" in outputs[-1]


@pytest.mark.asyncio
async def test_replenishment_failure_still_sends_and_charges_available_images():
    harness = _SearchFlowHarness(illusts=[_illust(1), _illust(2)])
    harness.checkin_store, _ = _settle_store()
    harness._should_use_forward = lambda **kwargs: True
    harness._fetch_source_candidates = AsyncMock(side_effect=[
        (harness.illusts, 2, "lolicon:random"), RuntimeError("source unavailable"),
    ])
    harness.downloader = AsyncMock()
    harness.downloader.download_for_send.side_effect = [
        ("unused.jpg", "original", 1024), RuntimeError("图片超过大小上限"),
    ]
    event = _FlowEvent(platform="aiocqhttp")
    await _run_search(harness, event, "2")
    harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=20)
    assert "少 1 张" in str(event.sent[0][0].nodes[-1].content)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stage", ["download", "replenishment"])
async def test_total_time_budget_cancels_network_and_sends_available_image(tmp_path, blocked_stage):
    import asyncio
    harness = _SearchFlowHarness(illusts=[_illust(1), _illust(2)])
    harness.config["request_timeout"] = 0.01  # 测试替身压缩时间，生产下限仍为 5 秒。
    harness.checkin_store, _ = _settle_store()
    harness.downloader = AsyncMock()
    path = tmp_path / "get_px_ready.jpg"
    path.write_bytes(b"fixture")
    cancelled = []
    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)
    calls = 0
    async def download(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert kwargs["size_limit_bytes"] == 10 * 1024 * 1024
            return str(path), "original", 1024
        if blocked_stage == "download":
            return await blocked()
        raise RuntimeError("fixture")
    harness.downloader.download_for_send.side_effect = download
    fetch_calls = 0
    async def fetch(*args, **kwargs):
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls == 1:
            return harness.illusts, 2, "lolicon:random"
        return await blocked()
    harness._fetch_source_candidates = fetch
    await asyncio.wait_for(_run_search(harness, _FlowEvent(), "2"), timeout=1)
    assert cancelled == [True]
    assert calls == 2
    assert not path.exists()
    harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=20)


@pytest.mark.asyncio
@pytest.mark.parametrize("count, timeout", [(1, 30), (5, 5), (5, 30), (20, 30)])
async def test_total_budget_scales_with_requested_count(count, timeout):
    from types import SimpleNamespace
    clock = [0.0]
    harness = _SearchFlowHarness(illusts=[_illust(i) for i in range(count)])
    harness.config.update(max_count=count, request_timeout=timeout)
    harness.checkin_store, _ = _settle_store()
    harness.downloader = AsyncMock()
    async def download(*args, **kwargs):
        clock[0] += timeout * 0.9
        return "unused.jpg", "original", 1024
    harness.downloader.download_for_send.side_effect = download
    with patch("astrbot_plugin_get_px.pixiv.search.time", SimpleNamespace(monotonic=lambda: clock[0])):
        await _run_search(harness, _FlowEvent(), str(count))
    assert harness.downloader.download_for_send.await_count == count
    harness.checkin_store.spend_coins.assert_awaited_once_with(user_id="10001", cost=20 * count)
