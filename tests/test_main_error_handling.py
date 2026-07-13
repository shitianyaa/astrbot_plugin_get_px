import asyncio
import json
import inspect
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from quart import Quart
from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402
from astrbot_plugin_get_px.checkin.application import (  # noqa: E402
    CheckinApplicationMixin,
)
from astrbot_plugin_get_px.checkin.artwork import CheckinArtworkMixin  # noqa: E402
from astrbot_plugin_get_px.checkin import (  # noqa: E402
    CheckinProfile,
    CheckinRecord,
    CheckinResult,
)
from astrbot_plugin_get_px.checkin.card import CardBackground  # noqa: E402


class _FakeEvent:
    def __init__(self, order=None, *, fail_send=False):
        self.order = order if order is not None else []
        self.fail_send = fail_send
        self.sent = []
        self.unified_msg_origin = "private:10001"

    def get_sender_id(self):
        return "10001"

    def get_sender_name(self):
        return "Alice"

    def get_group_id(self):
        return ""

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


class _FailingClient:
    async def illust_detail(self, _illust_id):
        raise RuntimeError(r"token expired at C:\secret\pixiv.db")


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
        background_source="rank:week" if with_background else "",
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
        template_version="v2",
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

    def cache_key(self, **kwargs):
        self.key_inputs.append(kwargs)
        return "a" * 64

    def get(self, date_key, key):
        self.order.append("cache_get")
        self.get_calls.append((date_key, key))
        return self.cache_path if self.hit else None

    async def store(self, date_key, key, renderer):
        self.order.append("cache_store")
        self.store_calls.append((date_key, key))
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

    async def test_handle_info_hides_pixiv_detail_exception(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.client = _FailingClient()

        results = await _collect(plugin._handle_info(_FakeEvent(), 123456))

        self.assertEqual(results, ["❌ 获取作品详情失败，请稍后再试"])

    async def test_handle_download_hides_pixiv_detail_exception(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.client = _FailingClient()
        plugin._check_rate_limit = lambda _sender_id: 0

        results = await _collect(plugin._handle_download(_FakeEvent(), 123456))

        self.assertEqual(results, ["❌ 获取作品详情失败，请稍后再试"])

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
                source="rank:week",
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
                source="rank:week",
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
                source="rank:week",
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
            self.assertTrue(plugin.checkin_cache.cache_path.exists())
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
                source="rank:week",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            final_cache = plugin.checkin_cache.cache_path

            async def cancel_after_cache_publish(*_args):
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
                source="rank:week",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            rendered = Path(tmp) / "rendered.jpg"
            plugin._render_checkin_card.return_value = _make_card(rendered)
            plugin._record_checkin_background.side_effect = asyncio.CancelledError()

            with self.assertRaises(asyncio.CancelledError):
                await _collect(plugin._handle_checkin(_FakeEvent(order)))

            plugin._release_checkin_background_claim.assert_awaited_once()
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
            plugin._record_sent_image = AsyncMock()
            plugin._record_image_usage = AsyncMock()
            source = Path(tmp) / "selected.png"
            PILImage.new("RGB", (750, 1000), (40, 80, 160)).save(source)
            plugin._prepare_checkin_background.return_value = CardBackground(
                image_path=str(source),
                mode="pixiv_daily",
                source="rank:week",
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
            plugin._record_sent_image.assert_not_awaited()
            plugin._record_image_usage.assert_awaited_once()
            usage_args = plugin._record_image_usage.await_args
            self.assertEqual(usage_args.args[1], "rank:week")
            self.assertEqual(str(usage_args.args[2]["id"]), "445566")
            self.assertEqual(usage_args.kwargs["feature"], "checkin")
            self.assertEqual(usage_args.kwargs["user_id"], "10001")

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
                    source="rank:week",
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
