import asyncio
import json
import inspect
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
from astrbot_plugin_get_px.checkin.application import (  # noqa: E402
    CheckinApplicationMixin,
)
from astrbot_plugin_get_px.checkin.artwork import CheckinArtworkMixin  # noqa: E402
from astrbot_plugin_get_px.checkin import (  # noqa: E402
    CheckinProfile,
    CheckinRecord,
    CheckinResult,
    UnversionedCheckinDatabaseError,
)
from astrbot_plugin_get_px.checkin.card import CardBackground  # noqa: E402


class _FakeEvent:
    def __init__(self, order=None, *, fail_send=False):
        self.order = order if order is not None else []
        self.fail_send = fail_send
        self.sent = []
        self.unified_msg_origin = "private:10001"
        self.stopped = False

    def get_sender_id(self):
        return "10001"

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return ""

    def get_platform_name(self):
        return "aiocqhttp"

    def get_self_id(self):
        return "20001"

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
        self.render_tier_updates = []

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

    async def update_record_render_tier(self, **kwargs):
        self.render_tier_updates.append(kwargs["render_tier"])
        return replace(self.result.record, render_tier=kwargs["render_tier"])


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

    def cache_key(self, **kwargs):
        self.key_inputs.append(kwargs)
        return "a" * 64

    def get(self, date_key, key, *, expected_size=(960, 540)):
        self.order.append("cache_get")
        self.get_calls.append((date_key, key, expected_size))
        return self.cache_path if self.hit else None

    async def store(self, date_key, key, renderer, *, expected_size=(960, 540)):
        self.order.append("cache_store")
        self.store_calls.append((date_key, key, expected_size))
        rendered_path = Path(await renderer())
        if self.fail_store:
            raise ValueError("invalid rendered card")
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(rendered_path, self.cache_path)
        self.hit = True
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
    plugin.image_index = SimpleNamespace(retention_days=1)
    cache_path = Path(tmp) / "cache" / "card.jpg"
    if cache_hit:
        _make_card(cache_path)
    plugin.checkin_cache = _FakeCache(cache_path, order, hit=cache_hit)
    plugin._prepare_checkin_background = AsyncMock()
    plugin._restore_checkin_background = AsyncMock()
    plugin._render_checkin_card = AsyncMock()

    async def record_usage(_event, background):
        if background and background.image_path:
            order.append("usage")

    plugin._record_checkin_background = AsyncMock(side_effect=record_usage)
    plugin._release_checkin_background_claim = AsyncMock(
        side_effect=lambda *_: order.append("release_claim")
    )
    return plugin


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
    def _plugin_for_search_usage_cancellation(
        self,
        tmp: str,
        illusts: list[dict],
        *,
        send_as_forward: bool,
    ):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {
            "rate_limit_seconds": 0,
            "max_count": len(illusts),
            "request_timeout": 30,
            "image_quality": "original",
            "auto_downgrade_original_mb": 3,
            "filter_manga": False,
            "dedupe_days": 1,
            "send_as_forward": send_as_forward,
        }
        plugin._last_request = {}
        plugin._fetch_source_candidates = AsyncMock(
            return_value=(illusts, len(illusts), "lolicon:random")
        )
        plugin._filter_blacklisted_illusts = AsyncMock(return_value=illusts)
        plugin._pick_illusts = AsyncMock(return_value=illusts)
        paths = []
        for illust in illusts:
            path = Path(tmp) / f"{illust['id']}.jpg"
            path.write_bytes(b"image")
            paths.append((str(path), "original", 5))
        plugin.downloader = SimpleNamespace(
            download_for_send=AsyncMock(side_effect=paths)
        )
        plugin.image_index = SimpleNamespace(
            retention_days=1,
            release_usage=AsyncMock(),
        )
        plugin._record_image_usage = AsyncMock(side_effect=asyncio.CancelledError())
        return plugin

    async def test_per_image_send_cancellation_while_recording_keeps_pending_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            illusts = [{"id": 101, "title": "First"}]
            plugin = self._plugin_for_search_usage_cancellation(
                tmp, illusts, send_as_forward=False
            )
            event = _FakeEvent()

            with self.assertRaises(asyncio.CancelledError):
                await _collect(plugin._handle_search(event, "", "1"))

            self.assertEqual(len(event.sent), 1)
            plugin.image_index.release_usage.assert_not_awaited()

    async def test_forward_send_cancellation_while_recording_keeps_all_pending_usage(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            illusts = [
                {"id": 101, "title": "First"},
                {"id": 202, "title": "Second"},
            ]
            plugin = self._plugin_for_search_usage_cancellation(
                tmp, illusts, send_as_forward=True
            )
            event = _FakeEvent()

            with self.assertRaises(asyncio.CancelledError):
                await _collect(plugin._handle_search(event, "", "2"))

            self.assertEqual(len(event.sent), 1)
            plugin.image_index.release_usage.assert_not_awaited()

    async def test_per_image_send_success_is_logged_at_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            illusts = [{"id": 101, "title": "First"}]
            plugin = self._plugin_for_search_usage_cancellation(
                tmp, illusts, send_as_forward=False
            )
            plugin._record_image_usage = AsyncMock()
            event = _FakeEvent()

            with patch("astrbot_plugin_get_px.pixiv.search.logger") as mock_logger:
                output = await _collect(plugin._handle_search(event, "", "1"))

            self.assertEqual(output, [])
            messages = " ".join(
                str(call.args[0]) for call in mock_logger.info.call_args_list
            )
            self.assertIn("作品 101 已发送", messages)

    async def test_search_rate_limit_log_is_debug_and_does_not_include_user_id(self):
        plugin = object.__new__(GetPxPlugin)
        plugin._check_rate_limit = Mock(return_value=9)
        event = _FakeEvent()

        with patch("astrbot_plugin_get_px.pixiv.search.logger") as mock_logger:
            output = await _collect(plugin._handle_search(event, "private-tag", "1"))

        self.assertEqual(output, ["⏳ 请求太频繁，请 9 秒后再试"])
        mock_logger.warning.assert_not_called()
        message = str(mock_logger.debug.call_args.args[0])
        self.assertIn("retry_after_seconds=9", message)
        self.assertNotIn("10001", message)
        self.assertNotIn("private-tag", message)

    async def test_forward_fallback_send_success_is_logged_at_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            illusts = [{"id": 101, "title": "First"}]
            plugin = self._plugin_for_search_usage_cancellation(
                tmp, illusts, send_as_forward=True
            )
            plugin._record_image_usage = AsyncMock()
            event = _FakeEvent()
            event.send = AsyncMock(
                side_effect=[
                    RuntimeError("forward failed"),
                    RuntimeError("forward failed"),
                    RuntimeError("forward failed"),
                    None,
                    None,
                ]
            )

            with (
                patch("astrbot_plugin_get_px.pixiv.search.logger") as mock_logger,
                patch("astrbot_plugin_get_px.pixiv.search.asyncio.sleep", AsyncMock()),
            ):
                output = await _collect(plugin._handle_search(event, "", "1"))

            self.assertEqual(output, [])
            messages = " ".join(
                str(call.args[0]) for call in mock_logger.info.call_args_list
            )
            self.assertIn("[降级] 作品 101 已发送", messages)

    def test_legacy_disabled_dedupe_migrates_to_zero_days_once(self):
        class Config(dict):
            def __init__(self):
                super().__init__(dedupe_ttl_hours=0, dedupe_days=1)
                self.save_calls = 0

            def save_config(self):
                self.save_calls += 1

        plugin = object.__new__(GetPxPlugin)
        plugin.config = Config()

        self.assertEqual(plugin._migrate_dedupe_config(), 0)
        self.assertTrue(plugin.config["dedupe_days_migrated"])
        self.assertEqual(plugin.config.save_calls, 1)

        plugin.config["dedupe_days"] = 7
        self.assertEqual(plugin._migrate_dedupe_config(), 7)
        self.assertEqual(plugin.config.save_calls, 1)

    def test_legacy_enabled_dedupe_migrates_to_one_day(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {"dedupe_ttl_hours": 7, "dedupe_days": 5}

        self.assertEqual(plugin._migrate_dedupe_config(), 1)
        self.assertTrue(plugin.config["dedupe_days_migrated"])

    def test_dedupe_migration_save_failure_log_does_not_expose_exception_text(self):
        class Config(dict):
            def save_config(self):
                raise RuntimeError("https://private.example/config?token=secret")

        plugin = object.__new__(GetPxPlugin)
        plugin.config = Config(dedupe_ttl_hours=24)

        with patch("astrbot_plugin_get_px.main.logger") as mocked_logger:
            self.assertEqual(plugin._migrate_dedupe_config(), 1)

        warning = str(mocked_logger.warning.call_args.args[0])
        info = str(mocked_logger.info.call_args.args[0])
        self.assertIn("error_type=RuntimeError", warning)
        self.assertNotIn("private.example", warning)
        self.assertNotIn("secret", warning)
        self.assertIn("persisted=False", info)

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
        plugin._init_client = lambda: None

        class FakeImageIndex:
            async def cleanup_old_days(self, *, trigger="manual"):
                self.trigger = trigger
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

    def test_duplicate_penalty_formatter_is_removed(self):
        source = inspect.getsource(GetPxPlugin)

        self.assertNotIn("_format_duplicate_checkin_text", source)

    def test_checkin_flow_lock_is_user_scoped_across_midnight(self):
        source = inspect.getsource(CheckinApplicationMixin._handle_checkin)

        self.assertIn("lock_key = user_id", source)
        self.assertNotIn("CheckinStore.today_key()", source)

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

    def test_portrait_page_exhaustion_log_has_neutral_reason(self):
        source = inspect.getsource(
            CheckinArtworkMixin._download_checkin_pixiv_background
        )

        self.assertIn(
            "连续 {CHECKIN_BACKGROUND_PAGE_ATTEMPTS} 页无可用竖向作品", source
        )
        self.assertNotIn(
            "连续 {CHECKIN_BACKGROUND_PAGE_ATTEMPTS} 页候选均已使用", source
        )

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
            record = replace(
                _record(),
                background_quality="large",
                render_tier="清晰",
            )
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
                quality="medium",
            )
            plugin._restore_checkin_background.return_value = restored
            plugin._persist_checkin_render_tier = AsyncMock()
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
            self.assertEqual(len(plugin.checkin_store.background_updates), 1)
            self.assertEqual(
                plugin.checkin_store.background_updates[0]["quality"], "medium"
            )
            plugin._persist_checkin_render_tier.assert_not_awaited()
            self.assertLess(order.index("cache_get"), order.index("render"))
            self.assertLess(order.index("render"), order.index("send"))

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
                quality="large",
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
            self.assertEqual(
                plugin.checkin_store.background_updates[0]["quality"], "large"
            )
            self.assertEqual(len(plugin.checkin_greeting.calls), 1)
            self.assertLess(order.index("content:local"), order.index("ai"))
            self.assertLess(order.index("ai"), order.index("content:ai"))
            self.assertLess(order.index("render"), order.index("background_metadata"))
            self.assertLess(order.index("background_metadata"), order.index("send"))
            self.assertLess(order.index("send"), order.index("usage"))
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
            self.assertFalse(rendered.exists())

    async def test_first_checkin_persists_the_actual_fallback_render_tier(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record(persisted=False, with_background=False)
            plugin = _plugin_for_checkin(
                tmp, CheckinResult(_profile(), record, duplicate=False), order
            )
            plugin.config["checkin_card_quality_tier"] = "极致"
            plugin._prepare_checkin_background.return_value = None

            async def render(*_args, render_tier, **_kwargs):
                if render_tier == "极致":
                    raise RuntimeError("ultra unavailable")
                path = Path(tmp) / f"{render_tier}.jpg"
                PILImage.new("RGB", (1248, 702), (238, 224, 196)).save(
                    path, format="JPEG"
                )
                return str(path)

            plugin._render_checkin_card.side_effect = render

            output = await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertEqual(output, [])
            self.assertEqual(plugin.checkin_store.render_tier_updates, ["清晰"])

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

    async def test_remote_greeting_persist_failure_keeps_local_card_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            record = _record(persisted=False, with_background=False)
            plugin = _plugin_for_checkin(
                tmp, CheckinResult(_profile(), record, duplicate=False), order
            )
            original_update = plugin.checkin_store.update_record_content

            async def update_content(**kwargs):
                if kwargs["greeting_source"] == "ai":
                    raise RuntimeError("sqlite:///private/path")
                return await original_update(**kwargs)

            plugin.checkin_store.update_record_content = update_content

            with patch(
                "astrbot_plugin_get_px.checkin.application.logger"
            ) as mock_logger:
                updated = await plugin._prepare_checkin_record_content(
                    _FakeEvent(order), record, allow_ai=True
                )

            self.assertEqual(updated.greeting_source, "local")
            self.assertTrue(updated.greeting)
            message = str(mock_logger.warning.call_args.args[0])
            self.assertIn("stage=persist_upgrade", message)
            self.assertIn("error_type=RuntimeError", message)
            self.assertNotIn("private/path", message)

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

    async def test_send_failure_rolls_back_background_before_releasing_claim(self):
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
                plugin.checkin_store.background_updates[-1]["mode"], "fallback"
            )
            self.assertEqual(
                plugin.checkin_store.background_updates[-1]["illust_id"], ""
            )
            self.assertFalse(plugin.checkin_cache.cache_path.exists())
            plugin._record_checkin_background.assert_not_awaited()
            plugin._release_checkin_background_claim.assert_awaited_once()
            self.assertLess(
                max(
                    index
                    for index, item in enumerate(order)
                    if item == "background_metadata"
                ),
                order.index("release_claim"),
            )

    async def test_send_failure_with_dedupe_disabled_keeps_background_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            order = []
            result = CheckinResult(
                _profile(),
                _record(persisted=False, with_background=False),
                duplicate=False,
            )
            plugin = _plugin_for_checkin(tmp, result, order)
            plugin.image_index.retention_days = 0
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
            self.assertEqual(len(plugin.checkin_store.background_updates), 1)
            self.assertEqual(
                plugin.checkin_store.background_updates[0]["illust_id"], "445566"
            )
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
            plugin._record_checkin_background.assert_not_awaited()
            plugin._release_checkin_background_claim.assert_not_awaited()

    async def test_background_cleanup_does_not_log_success_when_file_remains(self):
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
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)

            with (
                patch("astrbot_plugin_get_px.checkin.application.cleanup"),
                patch("astrbot_plugin_get_px.checkin.application.logger") as logger,
            ):
                await _collect(plugin._handle_checkin(_FakeEvent(order)))

            self.assertTrue(source.exists())
            debug_messages = " ".join(
                str(call) for call in logger.debug.call_args_list
            )
            self.assertNotIn("签到背景临时文件清理完成", debug_messages)

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

    async def test_usage_cancellation_keeps_sent_claim_cleans_source_and_keeps_cache(
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

            plugin._release_checkin_background_claim.assert_not_awaited()
            self.assertFalse(source.exists())
            self.assertTrue(plugin.checkin_cache.cache_path.exists())

    async def test_usage_record_failure_after_send_does_not_emit_plain_fallback(self):
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
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)
            plugin._record_checkin_background.side_effect = RuntimeError(
                "database secret path"
            )
            event = _FakeEvent(order)

            with patch(
                "astrbot_plugin_get_px.checkin.application.logger"
            ) as mock_logger:
                output = await _collect(plugin._handle_checkin(event))

            self.assertEqual(output, [])
            self.assertEqual(len(event.sent), 1)
            plugin._release_checkin_background_claim.assert_not_awaited()
            messages = " ".join(
                str(call) for call in mock_logger.warning.call_args_list
            )
            self.assertIn("使用记录失败", messages)
            self.assertIn("error_type=RuntimeError", messages)
            self.assertNotIn("secret", messages)

    async def test_failed_send_keeps_claim_when_background_rollback_fails(self):
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
            original_update = store.update_record_background

            async def update_background(**kwargs):
                if kwargs["mode"] == "fallback":
                    raise RuntimeError("rollback failed")
                await original_update(**kwargs)

            store.update_record_background = update_background

            output = await _collect(
                plugin._handle_checkin(_FakeEvent(order, fail_send=True))
            )

            self.assertEqual(len(output), 1)
            self.assertEqual(plugin._prepare_checkin_background.await_count, 1)
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
            plugin._record_checkin_background.assert_not_awaited()
            plugin._release_checkin_background_claim.assert_not_awaited()

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
