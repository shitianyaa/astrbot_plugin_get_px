import asyncio
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from quart import Quart
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.main import GetPxPlugin, PLUGIN_VERSION  # noqa: E402
import astrbot_plugin_get_px.main as main_module  # noqa: E402
from astrbot_plugin_get_px.pixiv.constants import MAX_IMAGE_COUNT  # noqa: E402
from astrbot_plugin_get_px.checkin import (  # noqa: E402
    CheckinProfile,
    CheckinRecord,
    CheckinResult,
    UnversionedCheckinDatabaseError,
)
from astrbot_plugin_get_px.checkin.card import CardBackground  # noqa: E402
from astrbot_plugin_get_px.checkin.cache import CheckinCardCache  # noqa: E402
from astrbot_plugin_get_px.checkin.store import CheckinStore  # noqa: E402
from astrbot_plugin_get_px.pixiv.safety import ContentSafetyPolicy  # noqa: E402


class _FakeEvent:
    def __init__(self, order=None, *, fail_send=False, group_id=""):
        self.order = order if order is not None else []
        self.fail_send = fail_send
        self.group_id = group_id
        self.sent = []
        self.unified_msg_origin = "private:10001"
        self.stopped = False

    def get_sender_id(self):
        return "10001"

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return self.group_id

    def get_platform_name(self):
        return "aiocqhttp"

    def plain_result(self, text):
        return text

    def chain_result(self, chain):
        return chain

    async def send(self, payload):
        self.order.append("send")
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(payload)

    def stop_event(self):
        self.stopped = True


class _UserEvent(_FakeEvent):
    def __init__(self, user_id, order=None):
        super().__init__(order)
        self._user_id = user_id
        self.unified_msg_origin = f"private:{user_id}"

    def get_sender_id(self):
        return self._user_id

    def get_sender_name(self):
        return f"User{self._user_id}"


async def _collect(async_iterable):
    return [item async for item in async_iterable]


def _profile() -> CheckinProfile:
    return CheckinProfile(
        user_id="10001",
        coins=180,
        affection=12.5,
        total_days=3,
        streak_days=3,
        last_checkin_date="2026-07-11",
        boost_start_date="",
        boost_until_date="",
        repeat_penalty_date="",
        repeat_penalty_total=0.0,
        created_at="2026-07-11T08:00:00+08:00",
        updated_at="2026-07-11T08:00:00+08:00",
    )


def _record(*, persisted=True, with_background=True) -> CheckinRecord:
    return CheckinRecord(
        date_key="2026-07-11",
        user_id="10001",
        username="Alice",
        bot_name="neko",
        base_coins=80,
        bonus_coins=0,
        coins_reward=80,
        base_affection=0.8,
        bonus_affection=0.0,
        affection_reward=0.8,
        boost_active=False,
        boost_multiplier=1.0,
        total_coins_after=180,
        total_affection_after=12.5,
        total_days_after=3,
        streak_days_after=3,
        note="今日小记",
        background_mode="pixiv_daily" if with_background else "",
        background_source="pixiv:recommended" if with_background else "",
        background_illust_id="445566" if with_background else "",
        background_title="Blue Sky" if with_background else "",
        background_author="Someone" if with_background else "",
        created_at="2026-07-11T08:00:00+08:00",
        updated_at="2026-07-11T08:00:00+08:00",
        event_key="normal" if persisted else "",
        event_label="",
        greeting="今天也见面了" if persisted else "",
        greeting_source="local",
        secondary_note="",
        template_version="default:1",
    )


class _FakeCheckinStore:
    def __init__(self, result: CheckinResult, order):
        self.result = result
        self.order = order
        self.content_updates = []
        self.background_updates = []

    async def checkin(self, **_kwargs):
        self.order.append("checkin")
        return self.result

    async def update_record_content(self, **kwargs):
        source = kwargs["greeting_source"]
        self.order.append(f"content:{source}")
        self.content_updates.append(kwargs)
        return replace(
            self.result.record,
            event_key=kwargs["event_key"],
            event_label=kwargs["event_label"],
            greeting=kwargs["greeting"],
            greeting_source=source,
            greeting_attribution=kwargs.get("greeting_attribution", ""),
            secondary_note=kwargs["secondary_note"],
            template_version=kwargs["template_version"],
        )

    async def update_record_background(self, **kwargs):
        self.order.append("background_metadata")
        self.background_updates.append(kwargs)


class _FakeGreetingGenerator:
    def __init__(self, order):
        self.order = order
        self.calls = []

    async def generate(self, event, context, **kwargs):
        self.order.append("ai")
        self.calls.append((event, context, kwargs))
        return "AI 今天也很高兴见到你", "ai"

    async def generate_hitokoto(self, context, **kwargs):
        self.order.append("hitokoto")
        self.calls.append((context, kwargs))
        return (
            "每一天都是新的一页。",
            "hitokoto",
            "毛不易 · 芬芳一生",
        )


class _FakeCache:
    def __init__(self, cache_path: Path, order, *, hit=False, fail_store=False):
        self.cache_path = cache_path
        self.order = order
        self.hit = hit
        self.fail_store = fail_store
        self.get_calls = []
        self.store_calls = []
        self.key_inputs = []
        self.backgrounds = {}

    def get_background(self, date_key, key):
        return self.backgrounds.get((date_key, key))

    def store_background(self, date_key, key, background):
        self.backgrounds[date_key, key] = replace(background, image_path="", illust=None)

    def cache_key(self, **kwargs):
        self.key_inputs.append(kwargs)
        return "a" * 64

    def get(self, date_key, key, *, expected_size=None):
        self.order.append("cache_get")
        self.get_calls.append((date_key, key))
        return self.cache_path if self.hit else None

    async def store(self, date_key, key, renderer, *, expected_size=None):
        self.order.append("cache_store")
        self.store_calls.append((date_key, key))
        rendered_path = Path(await renderer())
        if self.fail_store:
            raise ValueError("invalid rendered card")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rendered_path, self.cache_path)
        self.hit = True
        return self.cache_path


class _PolicyAwareFakeCache(_FakeCache):
    def __init__(self, cache_path: Path, order):
        super().__init__(cache_path, order)
        self.entries: set[str] = set()

    def cache_key(self, **kwargs):
        self.key_inputs.append(kwargs)
        return json.dumps(kwargs, ensure_ascii=False, sort_keys=True, default=str)

    def get(self, date_key, key, *, expected_size=None):
        self.order.append("cache_get")
        self.get_calls.append((date_key, key))
        return self.cache_path if key in self.entries else None

    async def store(self, date_key, key, renderer, *, expected_size=None):
        self.order.append("cache_store")
        self.store_calls.append((date_key, key))
        rendered_path = Path(await renderer())
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rendered_path, self.cache_path)
        self.entries.add(key)
        return self.cache_path


def _make_card(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    PILImage.new("RGB", (960, 540), (238, 224, 196)).save(path, format="JPEG")
    return str(path)


def _plugin_for_checkin(tmp: str, result: CheckinResult, order, *, cache_hit=False):
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {
        "checkin_enabled": True,
        "checkin_bot_name": "neko",
        "checkin_avatar_enabled": False,
        "checkin_greeting_mode": "ai",
        "checkin_ai_greeting_provider_id": "provider-1",
        "checkin_ai_greeting_prompt": "prompt",
        "checkin_ai_greeting_timeout": 8.0,
    }
    plugin.checkin_store = _FakeCheckinStore(result, order)
    plugin.checkin_greeting = _FakeGreetingGenerator(order)
    cache_path = Path(tmp) / "cache" / "card.jpg"
    if cache_hit:
        _make_card(cache_path)
    plugin.checkin_cache = _FakeCache(cache_path, order, hit=cache_hit)
    plugin._prepare_checkin_background = AsyncMock()
    plugin._restore_checkin_background = AsyncMock()
    plugin._render_checkin_card = AsyncMock()
    plugin.image_index = SimpleNamespace(
        retention_days=7,
        get_custom_safety_terms=AsyncMock(return_value=set()),
        get_blacklisted_illust_ids=AsyncMock(return_value=set()),
    )
    plugin.image_index.get_safety_snapshot = AsyncMock(
        side_effect=lambda: (
            plugin.image_index.get_custom_safety_terms.return_value,
            plugin.image_index.get_blacklisted_illust_ids.return_value,
        )
    )

    async def record_usage(_event, background):
        if background and background.image_path:
            order.append("usage")

    plugin._record_checkin_background = AsyncMock(side_effect=record_usage)
    plugin._release_checkin_background_claim = AsyncMock(
        side_effect=lambda *_: order.append("release_claim")
    )
    return plugin


class Issue24CacheTest(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_background_uses_consistent_placeholder_and_cache(self):
        from astrbot_plugin_get_px.checkin import card

        with tempfile.TemporaryDirectory() as tmp:
            plugin, _selected = self.setup_plugin(tmp)
            source = Path(tmp) / "rejected.png"
            PILImage.new("RGB", (6, 8)).save(source)
            plugin._prepare_checkin_background.side_effect = None
            plugin._prepare_checkin_background.return_value = CardBackground(
                mode="pixiv_daily", source="lolicon:random", image_path=str(source),
                illust_id="999999:0", title="RejectedArtwork", author="RejectedArtist",
            )
            del plugin._prepare_checkin_card_data
            rendered = []

            async def render(_event, **kwargs):
                rendered.append(kwargs["prepared_data"])
                return _make_card(Path(tmp) / "rendered.jpg")

            plugin._render_checkin_card.side_effect = render
            with patch.object(card, "_file_to_data_url", return_value=""):
                event = _FakeEvent(group_id="a")
                self.assertEqual(await _collect(plugin._handle_checkin(event)), [])
            self.assertEqual(len(rendered), 1)
            for field in ("artwork_url", "artwork_title", "artwork_author", "artwork_id"):
                self.assertEqual(rendered[0][field], "")
            self.assertNotIn("Rejected", rendered[0]["artwork_credit"])
            self.assertEqual(len(event.sent), 1)
            self.assertEqual(len(event.sent[0]), 1)
            summaries = list(plugin.checkin_cache.root.rglob("*.json"))
            self.assertEqual(len(summaries), 1)
            summary = json.loads(summaries[0].read_text(encoding="utf-8"))
            self.assertEqual(summary["mode"], "fallback")
            self.assertEqual(summary["illust_id"], "")
            plugin._release_checkin_background_claim.assert_awaited_once()
            used_background = plugin._record_checkin_background.await_args.args[1]
            self.assertEqual(used_background.mode, "fallback")
            self.assertEqual(used_background.illust_id, "")
            self.assertFalse(source.exists())
            # 摘要命中后直接复用占位卡，不再恢复或重新抽取被拒绝的作品。
            self.assertEqual(await _collect(plugin._handle_checkin(_FakeEvent(group_id="a"))), [])
            self.assertEqual(len(rendered), 1)

    async def test_paid_refresh_rejects_encoding_before_charge(self):
        from astrbot_plugin_get_px.checkin import shop

        with tempfile.TemporaryDirectory() as tmp:
            plugin, _selected = self.setup_plugin(tmp)
            await plugin.checkin_store.checkin(user_id="10001", username="Alice", bot_name="neko")
            await plugin.checkin_store.update_checkin_member(
                user_id="10001", coins=500, affection=0, total_days=1, streak_days=1,
            )
            before = await plugin.checkin_store.get_today_record("10001")
            source = Path(tmp) / "rejected.png"
            PILImage.new("RGB", (6, 8)).save(source)
            plugin._prepare_checkin_background.side_effect = None
            plugin._prepare_checkin_background.return_value = CardBackground(
                mode="pixiv_daily", source="lolicon:random", image_path=str(source),
                illust_id="999999:0", title="RejectedArtwork",
            )
            with patch.object(shop, "_file_to_data_url", return_value=""):
                output = await _collect(plugin._handle_refresh_checkin_background(_FakeEvent()))
            self.assertIn("不扣金币", output[0])
            self.assertEqual((await plugin.checkin_store.get_profile("10001")).coins, 500)
            self.assertEqual(await plugin.checkin_store.get_today_record("10001"), before)
            plugin._release_checkin_background_claim.assert_awaited_once()
            self.assertFalse(source.exists())

    def setup_plugin(self, tmp):
        plugin = _plugin_for_checkin(
            tmp, CheckinResult(_profile(), _record(), duplicate=True), []
        )
        plugin.config["checkin_greeting_mode"] = "local"
        plugin.checkin_store = CheckinStore(Path(tmp) / "data")
        plugin.checkin_cache = CheckinCardCache(Path(tmp) / "cache")
        # Exercise the actual no-token Lolicon restore path, without network calls.
        del plugin._restore_checkin_background
        plugin._content_safety_policy = AsyncMock(
            side_effect=lambda event: ContentSafetyPolicy(
                general_only_enabled=event.group_id != "relaxed", group_id=event.group_id
            )
        )
        selected = []

        async def select(event, _record, **_kwargs):
            illust_id = f"{110000 + len(selected)}:0"
            selected.append(illust_id)
            return CardBackground(
                mode="pixiv_daily", source="lolicon:random", illust_id=illust_id,
                title=f"Artwork {illust_id}", author="Artist", quality="large",
            )

        plugin._prepare_checkin_background.side_effect = select
        plugin._prepare_checkin_card_data = AsyncMock(return_value={})

        async def render(_event, **kwargs):
            path = Path(tmp) / "rendered.jpg"
            shade = int(kwargs["background"].illust_id.split(":")[0]) % 255
            PILImage.new("RGB", (960, 540), (shade, 40, 90)).save(path, "JPEG")
            return str(path)

        plugin._render_checkin_card.side_effect = render
        return plugin, selected

    async def send_card(self, plugin, group):
        event = _FakeEvent(group_id=group)
        self.assertEqual(await _collect(plugin._handle_checkin(event)), [])
        self.assertEqual(len(event.sent), 1)
        path = Path(event.sent[0][0].path)
        return path, path.read_bytes(), event.sent[0][1].text

    async def test_same_rules_cross_group_reuses_without_lolicon_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, selected = self.setup_plugin(tmp)
            first = await self.send_card(plugin, "a")
            snapshot = await plugin.checkin_store.get_today_record("10001")
            with patch.object(
                plugin, "_restore_checkin_background",
                wraps=plugin._restore_checkin_background,
            ) as restore:
                self.assertEqual(await self.send_card(plugin, "b"), first)
                self.assertEqual(await self.send_card(plugin, "a"), first)
                restore.assert_not_awaited()
            self.assertEqual(len(selected), 1)
            self.assertEqual(plugin._render_checkin_card.await_count, 1)
            self.assertEqual(await plugin.checkin_store.get_today_record("10001"), snapshot)

    async def test_snapshot_keeps_filter_rules_when_global_lists_change_mid_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, _ = self.setup_plugin(tmp)
            plugin.image_index.get_blacklisted_illust_ids.return_value = {"110000"}
            frozen, identity = await plugin._checkin_safety_snapshot(
                ContentSafetyPolicy(group_id="a")
            )
            plugin.image_index.get_blacklisted_illust_ids.return_value = set()
            blocked = await plugin._blacklist_reason_for_illust(
                {"id": 110000, "title": "safe", "x_restrict": 0}, "110000", frozen
            )
            plugin.image_index.get_blacklisted_illust_ids.return_value = {"110000"}
            self.assertIn("黑名单", blocked)
            self.assertEqual(identity["blacklisted_illust_ids"], ["110000"])
            plugin.image_index.get_safety_snapshot.assert_awaited_once()
            plugin.image_index.get_blacklisted_illust_ids.assert_not_awaited()

    async def test_disabled_independent_lists_do_not_split_identical_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, selected = self.setup_plugin(tmp)
            plugin._content_safety_policy.side_effect = lambda event: ContentSafetyPolicy(
                group_id=event.group_id, custom_terms=(event.group_id,),
                blacklisted_illust_ids=("123" if event.group_id == "a" else "456",),
            )
            first = await self.send_card(plugin, "a")
            self.assertEqual(await self.send_card(plugin, "b"), first)
            self.assertEqual(len(selected), 1)

    async def test_different_rules_keep_stable_variants_after_restart_and_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, selected = self.setup_plugin(tmp)
            first = await self.send_card(plugin, "a")
            snapshot = await plugin.checkin_store.get_today_record("10001")
            second = await self.send_card(plugin, "relaxed")
            self.assertNotEqual(second, first)
            self.assertIn(selected[1], second[2])
            self.assertEqual(await plugin.checkin_store.get_today_record("10001"), snapshot)
            plugin.checkin_cache = CheckinCardCache(Path(tmp) / "cache")
            results = await asyncio.gather(
                self.send_card(plugin, "a"), self.send_card(plugin, "relaxed")
            )
            self.assertEqual(results, [first, second])
            self.assertEqual(plugin._render_checkin_card.await_count, 2)
            self.assertEqual(len(selected), 2)

    async def test_global_rule_updates_and_clearing_rebuild_without_rewards(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, selected = self.setup_plugin(tmp)
            first = await self.send_card(plugin, "a")
            snapshot = await plugin.checkin_store.get_today_record("10001")
            plugin.image_index.get_blacklisted_illust_ids.return_value = {"110000"}
            second = await self.send_card(plugin, "a")
            self.assertNotEqual(second, first)
            self.assertEqual(await self.send_card(plugin, "a"), second)
            plugin.image_index.get_custom_safety_terms.return_value = {"new blocked term"}
            third = await self.send_card(plugin, "a")
            self.assertNotEqual(third, second)
            self.assertEqual(await self.send_card(plugin, "a"), third)
            shutil.rmtree(plugin.checkin_cache.root)
            self.assertNotEqual(await self.send_card(plugin, "a"), third)
            self.assertEqual(len(selected), 4)
            self.assertEqual(await plugin.checkin_store.get_today_record("10001"), snapshot)

    async def test_missing_corrupt_summary_or_jpeg_never_pairs_wrong_caption(self):
        for damage in ("summary_missing", "summary_corrupt", "jpeg_missing", "jpeg_corrupt"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as tmp:
                plugin, selected = self.setup_plugin(tmp)
                first = await self.send_card(plugin, "a")
                second = await self.send_card(plugin, "relaxed")
                if damage.startswith("summary"):
                    for path in plugin.checkin_cache.root.rglob("*.json"):
                        if damage == "summary_missing":
                            path.unlink()
                        else:
                            path.write_text('{"mode": 3}', encoding="utf-8")
                elif damage == "jpeg_missing":
                    second[0].unlink()
                else:
                    second[0].write_bytes(b"broken jpeg")
                third = await self.send_card(plugin, "relaxed")
                self.assertNotEqual(third, second)
                self.assertIn(selected[-1], third[2])
                self.assertEqual(await self.send_card(plugin, "relaxed"), third)
                self.assertEqual(await self.send_card(plugin, "a"), first)

    async def test_refresh_and_theme_change_do_not_reuse_old_variants(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin, selected = self.setup_plugin(tmp)
            await self.send_card(plugin, "a")
            second = await self.send_card(plugin, "relaxed")
            purchase = await plugin.checkin_store.purchase_background_refresh(
                user_id="10001", cost=0, mode="pixiv_daily", source="lolicon:random",
                illust_id="220000:0", title="Refresh", author="Artist", quality="large",
            )
            self.assertTrue(purchase.success)
            third = await self.send_card(plugin, "relaxed")
            self.assertNotEqual(third, second)
            self.assertEqual(await self.send_card(plugin, "relaxed"), third)
            await plugin.checkin_store.purchase_theme(user_id="10001", theme_id="yellow", cost=0)
            await plugin.checkin_store.select_theme(user_id="10001", theme_id="yellow")
            self.assertNotEqual(await self.send_card(plugin, "relaxed"), third)
            self.assertEqual(len(selected), 4)


class _ConcurrentCheckinStore(_FakeCheckinStore):
    def __init__(self, first_result: CheckinResult, order):
        super().__init__(first_result, order)
        self.checkin_calls = 0
        self.current_record = first_result.record

    async def checkin(self, **_kwargs):
        self.checkin_calls += 1
        self.order.append(f"checkin:{self.checkin_calls}")
        if self.checkin_calls == 1:
            return self.result
        return CheckinResult(_profile(), self.current_record, duplicate=True)

    async def update_record_content(self, **kwargs):
        updated = await super().update_record_content(**kwargs)
        self.current_record = updated
        return updated

    async def update_record_background(self, **kwargs):
        await super().update_record_background(**kwargs)
        self.current_record = replace(
            self.current_record,
            background_mode=kwargs["mode"],
            background_source=kwargs["source"],
            background_illust_id=kwargs["illust_id"],
            background_title=kwargs["title"],
            background_author=kwargs["author"],
        )


class MainErrorHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_terminate_calls_wait_for_same_cleanup(self):
        plugin = object.__new__(GetPxPlugin)
        plugin._termination_task = None
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_calls = 0

        async def cleanup():
            nonlocal cleanup_calls
            cleanup_calls += 1
            cleanup_started.set()
            await release_cleanup.wait()

        plugin._terminate_resources = cleanup
        first = asyncio.create_task(plugin.terminate())
        await cleanup_started.wait()
        second = asyncio.create_task(plugin.terminate())
        await asyncio.sleep(0)

        self.assertFalse(second.done())
        release_cleanup.set()
        await asyncio.gather(first, second)
        self.assertEqual(cleanup_calls, 1)

    async def test_cancelled_terminate_waiter_does_not_cancel_cleanup(self):
        plugin = object.__new__(GetPxPlugin)
        plugin._termination_task = None
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()
        cleanup_calls = 0

        async def cleanup():
            nonlocal cleanup_calls
            cleanup_calls += 1
            cleanup_started.set()
            await release_cleanup.wait()

        plugin._terminate_resources = cleanup
        first = asyncio.create_task(plugin.terminate())
        await cleanup_started.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(plugin.terminate())
        await asyncio.sleep(0)
        self.assertFalse(second.done())
        release_cleanup.set()
        await second
        self.assertEqual(cleanup_calls, 1)

    async def test_failed_terminate_cleanup_can_be_retried(self):
        plugin = object.__new__(GetPxPlugin)
        plugin._termination_task = None
        cleanup_calls = 0

        async def cleanup():
            nonlocal cleanup_calls
            cleanup_calls += 1
            if cleanup_calls == 1:
                raise RuntimeError("cleanup failed")

        plugin._terminate_resources = cleanup
        with self.assertRaisesRegex(RuntimeError, "cleanup failed"):
            await plugin.terminate()

        await plugin.terminate()
        self.assertEqual(cleanup_calls, 2)

    async def test_terminate_continues_after_individual_close_failures(self):
        for failing_resource in (
            "client",
            "lolicon_client",
            "downloader",
            "checkin_greeting",
            "image_index",
        ):
            with self.subTest(failing_resource=failing_resource):
                plugin = object.__new__(GetPxPlugin)
                plugin._holiday_refresh_task = None
                closers = {
                    "client": AsyncMock(),
                    "lolicon_client": AsyncMock(),
                    "downloader": AsyncMock(),
                    "checkin_greeting": AsyncMock(),
                    "image_index": Mock(),
                }
                closers[failing_resource].side_effect = RuntimeError(
                    f"{failing_resource} close failed"
                )
                plugin.client = SimpleNamespace(close=closers["client"])
                plugin.lolicon_client = SimpleNamespace(
                    close=closers["lolicon_client"]
                )
                plugin.downloader = SimpleNamespace(close=closers["downloader"])
                plugin.checkin_greeting = SimpleNamespace(
                    close=closers["checkin_greeting"]
                )
                plugin._last_request = {"user": 1.0}
                plugin._checkin_flow_locks = {"user": asyncio.Lock()}
                plugin.image_index = SimpleNamespace(close=closers["image_index"])
                plugin.checkin_store = object()

                await plugin._terminate_resources()

                for name in (
                    "client",
                    "lolicon_client",
                    "downloader",
                    "checkin_greeting",
                ):
                    closers[name].assert_awaited_once()
                closers["image_index"].assert_called_once()
                self.assertIsNone(plugin.client)
                self.assertIsNone(plugin.lolicon_client)
                self.assertIsNone(plugin.image_index)
                self.assertIsNone(plugin.checkin_store)
                self.assertEqual(plugin._last_request, {})
                self.assertEqual(plugin._checkin_flow_locks, {})

    async def test_search_command_accepts_empty_query(self):
        plugin = object.__new__(GetPxPlugin)
        plugin._ensure_client_or_error = lambda _event: True
        received = []

        async def handle_search(_event, *, tag, count_str):
            received.append((tag, count_str))
            yield "ok"

        plugin._handle_search = handle_search
        event = _FakeEvent()

        self.assertEqual(await _collect(plugin.cmd_p(event)), ["ok"])
        self.assertEqual(received, [("", "")])
        self.assertTrue(event.stopped)

    async def test_initialize_logs_v3_migration_guidance_for_old_database(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.downloader = SimpleNamespace(temp_dir=None, resume_cleanup=AsyncMock())
        plugin._init_client = lambda: None

        class FakeImageIndex:
            async def cleanup_old_days(self, *, trigger="manual"):
                return None

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch(
                "astrbot_plugin_get_px.main.StarTools.get_data_dir",
                return_value=tmp,
            ),
            patch(
                "astrbot_plugin_get_px.main.ImageIndexStore",
                return_value=FakeImageIndex(),
            ),
            patch("astrbot_plugin_get_px.main.logger") as mock_logger,
        ):
            with closing(sqlite3.connect(Path(tmp) / "checkin.sqlite3")) as conn:
                conn.execute("CREATE TABLE obsolete_data (value TEXT)")
                conn.commit()

            with self.assertRaises(UnversionedCheckinDatabaseError):
                await plugin.initialize()

        log_text = "\n".join(
            str(call.args[0]) for call in mock_logger.error.call_args_list
        )
        self.assertIn("缺少 schema 版本号", log_text)
        self.assertIn("3.0.0", log_text)
        self.assertIn(PLUGIN_VERSION, log_text)

    async def test_initialize_migrates_policy_before_convergence_and_logs_result(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.downloader = SimpleNamespace(temp_dir=None, resume_cleanup=AsyncMock())
        order = []
        plugin.config = {}
        plugin.context = SimpleNamespace()
        plugin._migrate_grouped_config = Mock()
        plugin._migrate_dedupe_config = Mock(return_value=1)
        plugin._init_client = Mock()
        plugin._cfg_bool = Mock(return_value=False)

        class FakeImageIndex:
            async def cleanup_old_days(self, *, trigger="manual"):
                return None

        class FakeStore:
            _db_path = Path("checkin.sqlite3")

            def __init__(self):
                order.append("store")

            def converge_legacy_group_policy_schema(self):
                order.append("converge")
                return {
                    "from_version": 3,
                    "to_version": 2,
                    "backup_path": "backup.sqlite3",
                    "changed": True,
                }

        class FakePolicyService:
            async def initialize(self, store):
                order.append("policy")
                return True

        class FakeCache:
            def __init__(self, *_args):
                order.append("cache")

            def cleanup_expired(self, **_kwargs):
                return None

        class FakeHoliday:
            def __init__(self, *_args, **_kwargs):
                order.append("holiday")

            async def refresh_if_due(self):
                return False

        class FakeWebApi:
            def register(self):
                order.append("register")

        plugin.group_safety_service = FakePolicyService()
        plugin.plugin_web_api = FakeWebApi()

        def discard_task(coro):
            coro.close()
            return Mock()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(main_module.StarTools, "get_data_dir", return_value=tmp),
            patch.object(main_module, "ImageIndexStore", return_value=FakeImageIndex()),
            patch.object(main_module, "CheckinStore", return_value=FakeStore()),
            patch.object(main_module, "CheckinCardCache", FakeCache),
            patch.object(main_module, "HolidayCalendar", FakeHoliday),
            patch.object(main_module.asyncio, "create_task", side_effect=discard_task),
            patch.object(main_module, "logger") as logger,
        ):
            await plugin.initialize()

        self.assertLess(order.index("policy"), order.index("converge"))
        self.assertLess(order.index("converge"), order.index("cache"))
        self.assertLess(order.index("cache"), order.index("register"))
        log_text = "\n".join(str(call.args[0]) for call in logger.info.call_args_list)
        self.assertIn("from_version=3", log_text)
        self.assertIn("to_version=2", log_text)
        self.assertIn("backup_path=backup.sqlite3", log_text)

    async def test_initialize_skips_convergence_when_policy_migration_fails(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.downloader = SimpleNamespace(temp_dir=None, resume_cleanup=AsyncMock())
        plugin.config = {}
        plugin.context = SimpleNamespace()
        plugin._migrate_grouped_config = Mock()
        plugin._migrate_dedupe_config = Mock(return_value=1)
        plugin._init_client = Mock()
        plugin._cfg_bool = Mock(return_value=False)
        order = []

        class FakeIndex:
            async def cleanup_old_days(self, *, trigger="manual"):
                return None

        class FakeStore:
            _db_path = Path("checkin.sqlite3")

            def converge_legacy_group_policy_schema(self):
                order.append("converge")
                raise AssertionError("convergence must be skipped")

        class FakePolicy:
            async def initialize(self, _store):
                return False

        plugin.group_safety_service = FakePolicy()
        plugin.plugin_web_api = SimpleNamespace(register=Mock())
        fake_cache = SimpleNamespace(cleanup_expired=Mock())
        fake_holiday = SimpleNamespace(refresh_if_due=AsyncMock(return_value=False))

        def discard_task(coro):
            coro.close()
            return Mock()

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(main_module.StarTools, "get_data_dir", return_value=tmp),
            patch.object(main_module, "ImageIndexStore", return_value=FakeIndex()),
            patch.object(main_module, "CheckinStore", return_value=FakeStore()),
            patch.object(main_module, "CheckinCardCache", return_value=fake_cache),
            patch.object(main_module, "HolidayCalendar", return_value=fake_holiday),
            patch.object(main_module.asyncio, "create_task", side_effect=discard_task),
        ):
            await plugin.initialize()
        self.assertEqual(order, [])

    async def test_initialize_stops_after_convergence_failure(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.downloader = SimpleNamespace(temp_dir=None, resume_cleanup=AsyncMock())
        plugin.config = {}
        plugin.context = SimpleNamespace()
        plugin._migrate_grouped_config = Mock()
        plugin._migrate_dedupe_config = Mock(return_value=1)
        plugin._init_client = Mock()
        plugin._cfg_bool = Mock(return_value=False)
        plugin.plugin_web_api = SimpleNamespace(register=Mock())
        order = []

        class FakeIndex:
            async def cleanup_old_days(self, *, trigger="manual"):
                return None

        class FakeStore:
            _db_path = Path("checkin.sqlite3")

            def converge_legacy_group_policy_schema(self):
                order.append("converge")
                raise RuntimeError("convergence failed")

        class FakePolicy:
            async def initialize(self, _store):
                return True

        plugin.group_safety_service = FakePolicy()
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(main_module.StarTools, "get_data_dir", return_value=tmp),
            patch.object(main_module, "ImageIndexStore", return_value=FakeIndex()),
            patch.object(main_module, "CheckinStore", return_value=FakeStore()),
            patch.object(main_module, "logger") as logger,
        ):
            with self.assertRaisesRegex(RuntimeError, "convergence failed"):
                await plugin.initialize()
        self.assertEqual(order, ["converge"])
        plugin.plugin_web_api.register.assert_not_called()
        error_text = "\n".join(
            str(call.args[0]) for call in logger.error.call_args_list
        )
        self.assertIn("会话策略数据库收敛失败", error_text)
        self.assertIn("error_type=RuntimeError", error_text)

    async def test_auto_trigger_stops_event_before_search(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {"auto_trigger_enabled": True}
        plugin.client = object()
        plugin._ensure_client_or_error = lambda _event: True

        async def handle_search(_event, *, tag, count_str):
            self.assertEqual(tag, "初音ミク")
            self.assertEqual(count_str, "3")
            yield "ok"

        plugin._handle_search = handle_search
        event = _FakeEvent()
        event.get_message_str = lambda: "来三张初音ミク图"

        self.assertEqual(await _collect(plugin.auto_trigger(event)), ["ok"])
        self.assertTrue(event.stopped)

    def test_friendly_send_error_is_callable_through_plugin_instance(self):
        plugin = object.__new__(GetPxPlugin)

        self.assertIn(
            "上传超时",
            plugin._friendly_send_error(asyncio.TimeoutError()),
        )

    def test_forward_threshold_uses_new_setting_or_legacy_bool_fallback(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {"forward_threshold": 0, "send_as_forward": False}
        self.assertEqual(plugin._forward_threshold(), 0)

        plugin.config = {"forward_threshold": 1}
        self.assertEqual(plugin._forward_threshold(), 1)

        plugin.config = {"send_as_forward": True}
        self.assertEqual(plugin._forward_threshold(), 0)

        plugin.config = {"send_as_forward": False}
        self.assertEqual(plugin._forward_threshold(), MAX_IMAGE_COUNT)

    def test_greeting_mode_defaults_to_hitokoto_and_accepts_explicit_sources(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {}
        self.assertEqual(plugin._checkin_greeting_mode(), "hitokoto")
        plugin.config["checkin_greeting_mode"] = "local"
        self.assertEqual(plugin._checkin_greeting_mode(), "local")
        plugin.config["checkin_greeting_mode"] = "ai"
        self.assertEqual(plugin._checkin_greeting_mode(), "ai")
        plugin.config["checkin_greeting_mode"] = "hitokoto"
        self.assertEqual(plugin._checkin_greeting_mode(), "hitokoto")
        plugin.config["checkin_greeting_mode"] = "auto"
        self.assertEqual(plugin._checkin_greeting_mode(), "hitokoto")

    async def test_web_internal_error_response_is_sanitized(self):
        plugin = object.__new__(GetPxPlugin)
        app = Quart(__name__)

        async with app.app_context():
            response, status = plugin._web_internal_error(
                "test", RuntimeError(r"C:\secret\pixiv.db is locked")
            )
            body = await response.get_data(as_text=True)

        payload = json.loads(body)
        self.assertEqual(status, 500)
        self.assertEqual(
            payload, {"success": False, "error": "服务内部错误，请稍后重试"}
        )
        self.assertNotIn("pixiv.db", body)
        self.assertNotIn("secret", body)

    async def test_duplicate_checkin_sends_cached_card_without_ai_or_artwork_selection(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(_profile(), _record(), duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order, cache_hit=True)
            event = _FakeEvent(order)

            output = await _collect(plugin._handle_checkin(event))

            self.assertEqual(output, [])
            self.assertEqual(len(event.sent), 1)
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
            self.assertEqual(plugin.checkin_greeting.calls, [])
            plugin._prepare_checkin_background.assert_not_awaited()
            plugin._restore_checkin_background.assert_not_awaited()
            plugin._render_checkin_card.assert_not_awaited()
            self.assertEqual(plugin.checkin_store.background_updates, [])
            self.assertEqual(order, ["checkin", "cache_get", "send"])

    async def test_duplicate_cache_miss_restores_same_artwork_and_rerenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record()
            result = CheckinResult(_profile(), record, duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "restored.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            restored = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source=record.background_source,
                illust_id=record.background_illust_id,
                title=record.background_title,
                author=record.background_author,
            )
            plugin._restore_checkin_background.return_value = restored
            rendered = Path(tmp) / "rendered.jpg"

            async def render(*_args, **_kwargs):
                order.append("render")
                return _make_card(rendered)

            plugin._render_checkin_card.side_effect = render

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            plugin._prepare_checkin_background.assert_not_awaited()
            plugin._restore_checkin_background.assert_awaited_once()
            self.assertEqual(
                plugin._restore_checkin_background.await_args.args[
                    1
                ].background_illust_id,
                "445566",
            )
            self.assertEqual(plugin.checkin_greeting.calls, [])
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
            self.assertEqual(plugin.checkin_store.background_updates, [])
            self.assertLess(order.index("cache_get"), order.index("render"))
            self.assertLess(order.index("render"), order.index("send"))

    async def _assert_duplicate_cache_policy_transition(
        self,
        *,
        first_event: _FakeEvent,
        first_policy: ContentSafetyPolicy,
        second_event: _FakeEvent,
        second_policy: ContentSafetyPolicy,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record()
            result = CheckinResult(_profile(), record, duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order)
            plugin.checkin_cache = _PolicyAwareFakeCache(
                Path(tmp) / "cache" / "card.jpg", order
            )
            plugin._content_safety_policy = AsyncMock(
                side_effect=[first_policy, second_policy]
            )
            restored = CardBackground(
                mode="fallback",
                source="fallback",
            )
            plugin._restore_checkin_background.return_value = restored
            rendered = Path(tmp) / "rendered.jpg"

            async def render(*_args, **_kwargs):
                order.append("render")
                return _make_card(rendered)

            plugin._render_checkin_card.side_effect = render
            first_event.order = order
            second_event.order = order

            self.assertEqual(await _collect(plugin._handle_checkin(first_event)), [])
            self.assertEqual(await _collect(plugin._handle_checkin(second_event)), [])

            self.assertEqual(plugin._render_checkin_card.await_count, 2)
            self.assertEqual(plugin._restore_checkin_background.await_count, 2)
            restore_policies = [
                call.kwargs["policy"]
                for call in plugin._restore_checkin_background.await_args_list
            ]
            self.assertEqual(
                restore_policies[0], (await plugin._checkin_safety_snapshot(first_policy))[0]
            )
            self.assertEqual(
                restore_policies[1], (await plugin._checkin_safety_snapshot(second_policy))[0]
            )
            cache_identities = {
                json.dumps(
                    item["view_model"]["content_safety_policy"],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                for item in plugin.checkin_cache.key_inputs
            }
            self.assertEqual(
                cache_identities,
                {
                    json.dumps(
                        await plugin._checkin_safety_identity(first_policy),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    json.dumps(
                        await plugin._checkin_safety_identity(second_policy),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            )
            self.assertEqual(len(plugin.checkin_cache.entries), 2)

    async def test_checkin_cache_separates_relaxed_group_from_strict_group(self):
        await self._assert_duplicate_cache_policy_transition(
            first_event=_FakeEvent(group_id="group-a"),
            first_policy=ContentSafetyPolicy(False, False, "group-a"),
            second_event=_FakeEvent(group_id="group-b"),
            second_policy=ContentSafetyPolicy(True, True, "group-b"),
        )

    async def test_checkin_cache_invalidates_when_same_group_reenables_safety(self):
        await self._assert_duplicate_cache_policy_transition(
            first_event=_FakeEvent(group_id="group-a"),
            first_policy=ContentSafetyPolicy(False, False, "group-a"),
            second_event=_FakeEvent(group_id="group-a"),
            second_policy=ContentSafetyPolicy(True, True, "group-a"),
        )

    async def test_checkin_cache_separates_relaxed_group_from_private_chat(self):
        await self._assert_duplicate_cache_policy_transition(
            first_event=_FakeEvent(group_id="group-a"),
            first_policy=ContentSafetyPolicy(False, False, "group-a"),
            second_event=_FakeEvent(),
            second_policy=ContentSafetyPolicy(),
        )

    async def test_duplicate_restore_failure_reselects_without_changing_daily_record(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record()
            result = CheckinResult(_profile(), record, duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order)
            policy = ContentSafetyPolicy(False, False, "group-a")
            plugin._content_safety_policy = AsyncMock(return_value=policy)
            plugin._restore_checkin_background.return_value = CardBackground(
                mode="fallback", source="fallback"
            )
            source = Path(tmp) / "reselected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            reselected = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="lolicon:random",
                illust_id="778899:0",
                title="New Art",
                author="Someone Else",
                quality="high",
            )

            async def prepare(*_args, **_kwargs):
                order.append("reselect_artwork")
                return reselected

            plugin._prepare_checkin_background.side_effect = prepare
            rendered = Path(tmp) / "rendered.jpg"

            async def render(*_args, **_kwargs):
                order.append("render")
                return _make_card(rendered)

            plugin._render_checkin_card.side_effect = render

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            plugin._restore_checkin_background.assert_awaited_once()
            plugin._prepare_checkin_background.assert_awaited_once()
            self.assertEqual(
                plugin._prepare_checkin_background.await_args.kwargs["policy"],
                (await plugin._checkin_safety_snapshot(policy))[0],
            )
            self.assertEqual(
                plugin.checkin_cache.key_inputs[0]["view_model"]["content_safety_policy"],
                await plugin._checkin_safety_identity(policy),
            )
            self.assertEqual(plugin.checkin_store.background_updates, [])
            self.assertEqual(
                next(iter(plugin.checkin_cache.backgrounds.values())).illust_id,
                "778899:0",
            )
            self.assertLess(
                order.index("cache_get"), order.index("reselect_artwork")
            )
            self.assertLess(order.index("reselect_artwork"), order.index("render"))
            self.assertLess(order.index("render"), order.index("send"))
            self.assertIn("usage", order)
            self.assertNotIn("release_claim", order)

    async def test_duplicate_reselect_send_failure_reverts_persist_and_releases_claim(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record()
            result = CheckinResult(_profile(), record, duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order)
            plugin._restore_checkin_background.return_value = CardBackground(
                mode="fallback", source="fallback"
            )
            source = Path(tmp) / "reselected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="lolicon:random",
                illust_id="778899:0",
                title="New Art",
                author="Someone Else",
                quality="high",
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            output = await _collect(
                plugin._handle_checkin(_FakeEvent(order, fail_send=True))
            )

            self.assertEqual(len(output), 1)
            self.assertEqual(plugin.checkin_store.background_updates, [])
            self.assertEqual(plugin.checkin_cache.backgrounds, {})
            self.assertFalse(plugin.checkin_cache.cache_path.exists())
            self.assertFalse(rendered.exists())
            plugin._record_checkin_background.assert_not_awaited()
            plugin._release_checkin_background_claim.assert_awaited_once()

    async def test_duplicate_restore_failure_without_reselect_keeps_placeholder(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record()
            result = CheckinResult(_profile(), record, duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order)
            plugin._restore_checkin_background.return_value = CardBackground(
                mode="fallback", source="fallback"
            )
            plugin._prepare_checkin_background.return_value = CardBackground(
                mode="fallback", source="fallback"
            )
            rendered = Path(tmp) / "rendered.jpg"

            async def render(*_args, **_kwargs):
                order.append("render")
                return _make_card(rendered)

            plugin._render_checkin_card.side_effect = render

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            plugin._prepare_checkin_background.assert_awaited_once()
            self.assertEqual(plugin.checkin_store.background_updates, [])
            self.assertNotIn("release_claim", order)
            self.assertNotIn("usage", order)
            self.assertLess(order.index("cache_get"), order.index("render"))
            self.assertTrue(plugin.checkin_cache.cache_path.exists())

    async def test_duplicate_without_recorded_background_does_not_reselect(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record(with_background=False)
            result = CheckinResult(_profile(), record, duplicate=True)
            plugin = _plugin_for_checkin(tmp, result, order)
            plugin._restore_checkin_background.return_value = CardBackground(
                mode="fallback", source="fallback"
            )
            rendered = Path(tmp) / "rendered.jpg"

            async def render(*_args, **_kwargs):
                order.append("render")
                return _make_card(rendered)

            plugin._render_checkin_card.side_effect = render

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            plugin._restore_checkin_background.assert_awaited_once()
            plugin._prepare_checkin_background.assert_not_awaited()
            self.assertEqual(plugin.checkin_store.background_updates, [])


    async def test_first_checkin_persists_content_then_rendered_artwork_and_usage_after_send(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record(persisted=False, with_background=False)
            result = CheckinResult(_profile(), record, duplicate=False)
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            selected = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="pixiv:recommended",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
                illust={"id": 445566, "width": 750, "height": 1000},
            )

            async def prepare(*_args, **_kwargs):
                order.append("select_artwork")
                return selected

            plugin._prepare_checkin_background.side_effect = prepare
            rendered = Path(tmp) / "rendered.jpg"

            async def render(*_args, **_kwargs):
                order.append("render")
                return _make_card(rendered)

            plugin._render_checkin_card.side_effect = render

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            self.assertEqual(
                [
                    update["greeting_source"]
                    for update in plugin.checkin_store.content_updates
                ],
                ["local", "ai"],
            )
            self.assertEqual(len(plugin.checkin_store.background_updates), 1)
            self.assertEqual(
                plugin.checkin_store.background_updates[0]["illust_id"], "445566"
            )
            self.assertEqual(len(plugin.checkin_greeting.calls), 1)
            self.assertLess(order.index("content:local"), order.index("ai"))
            self.assertLess(order.index("ai"), order.index("content:ai"))
            self.assertLess(order.index("render"), order.index("background_metadata"))
            self.assertLess(order.index("background_metadata"), order.index("send"))
            self.assertLess(order.index("send"), order.index("usage"))
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
            self.assertFalse(rendered.exists())

    async def test_hitokoto_mode_upgrades_local_greeting_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record(persisted=False, with_background=False)
            plugin = _plugin_for_checkin(
                tmp, CheckinResult(_profile(), record, duplicate=False), order
            )
            plugin.config["checkin_greeting_mode"] = "hitokoto"
            plugin.config["checkin_hitokoto_categories"] = ["动画", "诗词"]

            updated = await plugin._prepare_checkin_record_content(
                _FakeEvent(order), record, allow_ai=True
            )

            self.assertEqual(updated.greeting_source, "hitokoto")
            self.assertEqual(updated.greeting, "每一天都是新的一页。")
            self.assertEqual(updated.greeting_attribution, "毛不易 · 芬芳一生")
            self.assertEqual(
                [
                    item["greeting_source"]
                    for item in plugin.checkin_store.content_updates
                ],
                ["local", "hitokoto"],
            )
            self.assertIn("hitokoto", order)
            self.assertEqual(
                plugin.checkin_greeting.calls[-1][1]["categories"],
                ["动画", "诗词"],
            )

    async def test_render_failure_does_not_persist_artwork_or_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            background = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="pixiv:recommended",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            plugin._prepare_checkin_background.return_value = background
            plugin.checkin_cache.fail_store = True
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(len(output), 1)
            self.assertEqual(plugin.checkin_store.background_updates, [])
            plugin._record_checkin_background.assert_not_awaited()
            plugin._release_checkin_background_claim.assert_awaited_once()
            self.assertFalse(rendered.exists())

    async def test_cache_lookup_error_returns_text_and_releases_background_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(
                _profile(), _record(persisted=False, with_background=False), duplicate=False
            )
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), "blue").save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source), mode="pixiv_daily",
                source="lolicon:random", illust_id="123:0",
            )
            plugin._get_cached_checkin_card = AsyncMock(side_effect=OSError("cache unavailable"))
            event = _FakeEvent(order)
            output = await _collect(plugin._handle_checkin(event))
            self.assertEqual(len(output), 1)
            self.assertEqual(event.sent, [])
            plugin._release_checkin_background_claim.assert_awaited_once()
            plugin._render_checkin_card.assert_not_awaited()
            self.assertEqual(plugin.checkin_store.background_updates, [])
            self.assertFalse(source.exists())

    async def test_send_failure_keeps_cache_but_does_not_record_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="pixiv:recommended",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            output = await _collect(
                plugin._handle_checkin(_FakeEvent(order, fail_send=True))
            )

            self.assertEqual(len(output), 1)
            self.assertEqual(len(plugin.checkin_store.background_updates), 2)
            self.assertEqual(
                plugin.checkin_store.background_updates[1]["mode"], "fallback"
            )
            # 发送失败回滚：缓存与渲染中间文件均被清理。
            self.assertFalse(plugin.checkin_cache.cache_path.exists())
            self.assertFalse(rendered.exists())
            plugin._record_checkin_background.assert_not_awaited()
            plugin._release_checkin_background_claim.assert_awaited_once()

    async def test_cache_store_cancellation_releases_claim_and_cleans_pixiv_source(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="pixiv:recommended",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            final_cache = plugin.checkin_cache.cache_path
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            async def cancel_after_cache_publish(*_args, **_kwargs):
                _make_card(final_cache)
                raise asyncio.CancelledError()

            plugin.checkin_cache.store = AsyncMock(
                side_effect=cancel_after_cache_publish
            )

            with self.assertRaises(asyncio.CancelledError):
                await _collect(plugin._handle_checkin(_FakeEvent(order)))

            plugin._release_checkin_background_claim.assert_awaited_once()
            self.assertFalse(source.exists())
            self.assertTrue(final_cache.exists())

    async def test_usage_cancellation_releases_claim_cleans_source_and_keeps_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, result, order)
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="pixiv:recommended",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)
            plugin._record_checkin_background.side_effect = asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                await _collect(plugin._handle_checkin(_FakeEvent(order)))

            # 图片已发送，claim 保留（去重占用不释放）；临时源图已清理。
            plugin._release_checkin_background_claim.assert_not_awaited()
            self.assertFalse(source.exists())
            self.assertTrue(plugin.checkin_cache.cache_path.exists())

    async def test_failed_first_send_then_cached_resend_records_metadata_usage_once(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            first_result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, first_result, order)
            store = _ConcurrentCheckinStore(first_result, order)
            plugin.checkin_store = store
            del plugin._record_checkin_background
            plugin._record_image_usage = AsyncMock()
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="pixiv:recommended",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
                illust={"id": 445566, "title": "Blue Sky"},
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            first_output = await _collect(
                plugin._handle_checkin(_FakeEvent(order, fail_send=True))
            )
            second_output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(len(first_output), 1)
            self.assertEqual(second_output, [])
            self.assertEqual(plugin._prepare_checkin_background.await_count, 1)
            plugin._restore_checkin_background.assert_not_awaited()
            # 第一次发送失败回滚到 fallback 背景，第二次重发不记录图片使用。
            plugin._record_image_usage.assert_not_awaited()

    async def test_same_user_concurrent_checkins_wait_for_the_first_full_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            first_result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, first_result, order)
            store = _ConcurrentCheckinStore(first_result, order)
            plugin.checkin_store = store
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            prepare_started = asyncio.Event()
            finish_prepare = asyncio.Event()

            async def prepare(*_args, **_kwargs):
                prepare_started.set()
                await finish_prepare.wait()
                return CardBackground(
                    image_path=str(source),
                    mode="pixiv_daily",
                    source="pixiv:recommended",
                    illust_id="445566",
                    title="Blue Sky",
                    author="Someone",
                )

            plugin._prepare_checkin_background.side_effect = prepare
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            first = asyncio.create_task(
                _collect(plugin._handle_checkin(_FakeEvent(order)))
            )
            await prepare_started.wait()
            second = asyncio.create_task(
                _collect(plugin._handle_checkin(_FakeEvent(order)))
            )
            await asyncio.sleep(0.02)
            calls_before_first_finished = store.checkin_calls
            finish_prepare.set()
            await asyncio.gather(first, second)

            self.assertEqual(calls_before_first_finished, 1)
            self.assertEqual(store.checkin_calls, 2)

    async def test_concurrent_checkins_for_different_users_do_not_serialize(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            first_result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, first_result, order)
            store = _FakeCheckinStore(first_result, order)
            plugin.checkin_store = store
            entered = {"user-a": asyncio.Event(), "user-b": asyncio.Event()}
            release = {"user-a": asyncio.Event(), "user-b": asyncio.Event()}
            user_sequence = []

            async def checkin(**kwargs):
                user = kwargs["user_id"]
                user_sequence.append(user)
                entered[user].set()
                # 挂起在锁内：若流程锁是全局的，user-b 永远到不了这里。
                await release[user].wait()
                order.append(f"checkin:{user}")
                return replace(
                    first_result,
                    record=replace(first_result.record, user_id=user),
                )

            store.checkin = checkin

            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)

            async def prepare(*_args, **_kwargs):
                return CardBackground(
                    image_path=str(source),
                    mode="pixiv_daily",
                    source="pixiv:recommended",
                    illust_id="445566",
                    title="Blue Sky",
                    author="Someone",
                )

            plugin._prepare_checkin_background.side_effect = prepare
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            first = asyncio.create_task(
                _collect(plugin._handle_checkin(_UserEvent("user-a")))
            )
            await asyncio.wait_for(entered["user-a"].wait(), timeout=5)

            second = asyncio.create_task(
                _collect(plugin._handle_checkin(_UserEvent("user-b")))
            )
            # user-a 持锁挂起时，user-b 的 checkin 仍能进入，证明锁按用户隔离。
            await asyncio.wait_for(entered["user-b"].wait(), timeout=5)

            release["user-a"].set()
            release["user-b"].set()
            await asyncio.gather(first, second)

            self.assertEqual(user_sequence, ["user-a", "user-b"])

    def test_flow_lock_is_keyed_by_user(self):
        plugin = object.__new__(GetPxPlugin)
        lock_user_a_first_call = plugin._checkin_flow_lock("10001")
        lock_user_a_second_call = plugin._checkin_flow_lock("10001")
        lock_user_b = plugin._checkin_flow_lock("20002")

        self.assertIs(lock_user_a_first_call, lock_user_a_second_call)
        self.assertIsNot(lock_user_a_first_call, lock_user_b)

    async def test_duplicate_cache_identity_uses_record_snapshot_not_current_profile(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = replace(_record(), boost_active=True, boost_multiplier=2.0)
            current_profile = replace(
                _profile(),
                coins=9999,
                affection=88.8,
                total_days=99,
                streak_days=20,
                boost_start_date="2026-07-01",
                boost_until_date="2026-07-31",
            )
            plugin = _plugin_for_checkin(
                tmp,
                CheckinResult(current_profile, record, duplicate=True),
                order,
                cache_hit=True,
            )

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            view_model = plugin.checkin_cache.key_inputs[0]["view_model"]
            self.assertEqual(view_model["coins_total"], record.total_coins_after)
            self.assertEqual(
                view_model["affection_value_label"],
                f"{record.total_affection_after:.2f}",
            )
            self.assertEqual(view_model["total_days"], record.total_days_after)
            self.assertEqual(view_model["streak_days"], record.streak_days_after)
            self.assertEqual(view_model["boost_status_text"], "好感度奖励 ×2")


if __name__ == "__main__":
    unittest.main()
