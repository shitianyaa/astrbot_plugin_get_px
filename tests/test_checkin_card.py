import base64
from dataclasses import FrozenInstanceError, replace
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin import card as checkin_card  # noqa: E402
from astrbot_plugin_get_px.checkin import CheckinProfile, CheckinRecord  # noqa: E402
from astrbot_plugin_get_px.checkin.card import (  # noqa: E402
    CardBackground,
    _file_to_data_url,
)
from astrbot_plugin_get_px.checkin.cache import CheckinCardCache  # noqa: E402
from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402


def _profile() -> CheckinProfile:
    return CheckinProfile(
        user_id="123456789",
        coins=9999,
        affection=999.0,
        total_days=999,
        streak_days=999,
        last_checkin_date="2026-07-11",
        boost_start_date="2026-07-10",
        boost_until_date="2026-07-13",
        repeat_penalty_date="",
        repeat_penalty_total=0.0,
        created_at="2026-07-11T00:00:00",
        updated_at="2026-07-11T00:00:00",
    )


def _record() -> CheckinRecord:
    return CheckinRecord(
        date_key="2026-07-11",
        user_id="123456789",
        username="一位昵称很长但仍应安全显示的访客",
        bot_name="neko",
        base_coins=80,
        bonus_coins=20,
        coins_reward=100,
        base_affection=0.6,
        bonus_affection=0.4,
        affection_reward=1.0,
        boost_active=True,
        boost_multiplier=2.0,
        total_coins_after=321,
        total_affection_after=66.6,
        total_days_after=12,
        streak_days_after=5,
        note="旧字段不应覆盖每日问候",
        background_mode="pixiv_daily",
        background_source="daily",
        background_illust_id="445566",
        background_title="",
        background_author="",
        created_at="2026-07-11T00:00:00",
        updated_at="2026-07-11T00:00:00",
        event_key="birthday",
        event_label="七月生日",
        greeting="今天也有好好见面。",
        greeting_source="ai",
        secondary_note="累计签到达成纪念日",
    )


class CheckinCardViewModelTest(unittest.TestCase):
    def test_builds_h_card_from_persisted_record_snapshot(self):
        builder = getattr(checkin_card, "build_checkin_card_view_model", None)
        self.assertIsNotNone(builder, "缺少 H 卡片 view-model 构建器")

        view_model = builder(
            profile=_profile(),
            record=_record(),
            bot_name="neko",
            avatar_url="data:image/png;base64,avatar",
            background=CardBackground(
                illust_id="445566",
                title="这是一个超过十八个字符后必须安全省略的作品标题",
                author="这是一位名字非常非常长的作者名称",
            ),
        )

        self.assertEqual(view_model.coins_total, 321)
        self.assertEqual(view_model.affection_value, 66.6)
        self.assertEqual(view_model.total_days, 12)
        self.assertEqual(view_model.streak_days, 5)
        self.assertEqual(view_model.event_label, "七月生日")
        self.assertEqual(view_model.greeting, "今天也有好好见面。")
        self.assertEqual(view_model.greeting_source, "ai")
        self.assertLessEqual(len(view_model.badges), 1)
        self.assertNotIn("生日", view_model.badges)
        self.assertFalse(any(badge.startswith("×") for badge in view_model.badges))
        self.assertEqual(view_model.affection_next_text, "距离“信赖”还需 3.40")
        self.assertEqual(view_model.milestone_next_text, "月下常客 · 还差 18 天")
        self.assertLessEqual(len(view_model.artwork_title), 18)
        self.assertTrue(view_model.artwork_title.endswith("…"))
        self.assertLessEqual(len(view_model.artwork_author), 12)
        self.assertTrue(view_model.artwork_author.endswith("…"))
        with self.assertRaises(FrozenInstanceError):
            view_model.title = "changed"

    def test_card_data_does_not_expose_full_uid(self):
        data = checkin_card.build_checkin_card_data(
            profile=_profile(),
            record=_record(),
            bot_name="neko",
        )

        self.assertNotIn("user_id", data)
        self.assertNotIn("123456789", repr(data))
        self.assertIn("星期六", str(data["date_label"]))

    def test_hitokoto_greeting_uses_neutral_signature(self):
        view_model = checkin_card.build_checkin_card_view_model(
            profile=_profile(),
            record=replace(
                _record(),
                greeting_source="hitokoto",
                greeting_attribution="毛不易 · 芬芳一生",
            ),
            bot_name="neko",
        )

        self.assertEqual(view_model.bot_name, "毛不易 · 芬芳一生")

    def test_card_data_rejects_non_fixed_canvas_dimensions(self):
        for width, height in ((959, 540), (960, 539), (1200, 675), (960.5, 540)):
            with self.subTest(width=width, height=height):
                with self.assertRaises(ValueError):
                    checkin_card.build_checkin_card_data(
                        profile=_profile(),
                        record=_record(),
                        bot_name="neko",
                        width=width,
                        height=height,
                    )


class FileToDataUrlTest(unittest.TestCase):
    def test_preserves_validated_source_bytes_mime_dimensions_and_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "portrait.data"
            Image.new("RGBA", (750, 1000), (20, 90, 180, 160)).save(
                source, format="PNG"
            )
            source_bytes = source.read_bytes()
            source_mtime_ns = source.stat().st_mtime_ns

            data_url = _file_to_data_url(str(source))
            header, encoded = data_url.split(",", 1)
            decoded = base64.b64decode(encoded)

            self.assertEqual(header, "data:image/png;base64")
            self.assertEqual(decoded, source_bytes)
            with Image.open(BytesIO(decoded)) as image:
                self.assertEqual(image.size, (750, 1000))
                self.assertEqual(image.format, "PNG")
            self.assertEqual(source.read_bytes(), source_bytes)
            self.assertEqual(source.stat().st_mtime_ns, source_mtime_ns)

    def test_invalid_inputs_return_empty(self):
        self.assertEqual(_file_to_data_url(""), "")
        self.assertEqual(_file_to_data_url("/no/such/file.jpg"), "")
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.jpg"
            broken.write_bytes(b"not an image")
            self.assertEqual(_file_to_data_url(str(broken)), "")


class CheckinCardRenderQualityTest(unittest.IsolatedAsyncioTestCase):
    async def test_render_tiers_use_fixed_jpeg_quality_and_scale_options(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {"checkin_avatar_enabled": False}
        plugin.html_render = AsyncMock(return_value="card.jpg")

        for tier, scale_level in (
            ("省流量", None),
            ("清晰", "high"),
            ("极致", "ultra"),
        ):
            with self.subTest(tier=tier):
                result = await plugin._render_checkin_card(
                    SimpleNamespace(),
                    profile=_profile(),
                    record=_record(),
                    background=None,
                    bot_name="neko",
                    render_tier=tier,
                )

                self.assertEqual(result, "card.jpg")
                options = plugin.html_render.await_args.kwargs["options"]
                self.assertEqual(options["type"], "jpeg")
                self.assertEqual(options["quality"], 95)
                self.assertEqual(
                    options.get("device_scale_factor_level"), scale_level
                )

    def test_render_tier_changes_the_daily_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {"checkin_avatar_enabled": False}
            plugin.checkin_cache = CheckinCardCache(Path(tmp) / "cache")
            kwargs = {
                "profile": _profile(),
                "record": _record(),
                "background": None,
                "bot_name": "neko",
            }

            economy = plugin._checkin_card_cache_key(
                SimpleNamespace(), **kwargs, render_tier="省流量"
            )
            clear = plugin._checkin_card_cache_key(
                SimpleNamespace(), **kwargs, render_tier="清晰"
            )

            self.assertNotEqual(economy, clear)

    def test_persisted_background_quality_recreates_the_same_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {"checkin_avatar_enabled": False}
            plugin.checkin_cache = CheckinCardCache(Path(tmp) / "cache")
            record = replace(
                _record(),
                render_tier="清晰",
                background_mode="pixiv_daily",
                background_source="lolicon:tag",
                background_illust_id="445566",
                background_title="Blue Sky",
                background_author="Someone",
                background_quality="medium",
            )
            initial = CardBackground(
                mode="pixiv_daily",
                source="lolicon:tag",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
                quality="medium",
            )
            kwargs = {
                "profile": _profile(),
                "record": record,
                "bot_name": "neko",
                "render_tier": "清晰",
            }

            first_key = plugin._checkin_card_cache_key(
                SimpleNamespace(), background=initial, **kwargs
            )
            restored_key = plugin._checkin_card_cache_key(
                SimpleNamespace(),
                background=plugin._checkin_background_from_record(record),
                **kwargs,
            )

            self.assertEqual(first_key, restored_key)

    async def test_ultimate_render_failure_falls_back_to_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            attempts = []

            async def render(*_args, render_tier, **_kwargs):
                attempts.append(render_tier)
                if render_tier == "极致":
                    raise RuntimeError("ultra unavailable")
                path = Path(tmp) / f"{render_tier}.jpg"
                Image.new("RGB", (1248, 702), (238, 224, 196)).save(
                    path, format="JPEG"
                )
                return str(path)

            plugin._render_checkin_card = AsyncMock(side_effect=render)
            path, actual_tier = await plugin._render_checkin_card_with_fallback(
                SimpleNamespace(),
                profile=_profile(),
                record=_record(),
                background=None,
                bot_name="neko",
                preferred_tier="极致",
            )

            self.assertEqual(attempts, ["极致", "清晰"])
            self.assertEqual(actual_tier, "清晰")
            self.assertTrue(path.is_file())

    def test_current_theme_version_invalidates_an_older_daily_cache(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {
            "checkin_avatar_enabled": False,
        }
        plugin.checkin_cache = SimpleNamespace(cache_key=Mock(return_value="cache-key"))

        result = plugin._checkin_card_cache_key(
            SimpleNamespace(),
            profile=_profile(),
            record=replace(
                _record(),
                theme_id="yellow",
                template_version="yellow:old",
            ),
            background=None,
            bot_name="neko",
        )

        self.assertEqual(result, "cache-key")
        self.assertEqual(
            plugin.checkin_cache.cache_key.call_args.kwargs["template_version"],
            "yellow:1",
        )


class CheckinT2IProbeCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_probe_removes_render_source_after_success(self):
        from astrbot_plugin_get_px.scripts import probe_checkin_t2i_quality as probe

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "renderer-source.jpg"
            rendered = Image.new("RGB", (960, 540), (238, 224, 196))
            rendered.paste((40, 80, 160), (120, 90, 840, 450))
            rendered.save(source, format="JPEG")
            spec = next(iter(probe.CHECKIN_RENDER_TIERS.values()))
            with (
                patch.object(probe, "CHECKIN_RENDER_TIERS", {spec.name: spec}),
                patch.object(
                    probe.html_renderer,
                    "render_custom_template",
                    AsyncMock(return_value=str(source)),
                ),
            ):
                result = await probe.run(root / "output")

            self.assertFalse(source.exists())
            self.assertEqual(len(result["report"]), 1)
            self.assertTrue((root / "output" / "1_normal.jpg").is_file())

    async def test_probe_removes_render_source_when_copy_or_validation_fails(self):
        from astrbot_plugin_get_px.scripts import probe_checkin_t2i_quality as probe

        for failure_stage in ("copy", "inspect"):
            with self.subTest(failure_stage=failure_stage):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source = root / "renderer-source.jpg"
                    Image.new("RGB", (960, 540), (238, 224, 196)).save(
                        source, format="JPEG"
                    )
                    spec = next(iter(probe.CHECKIN_RENDER_TIERS.values()))
                    patches = [
                        patch.object(
                            probe,
                            "CHECKIN_RENDER_TIERS",
                            {spec.name: spec},
                        ),
                        patch.object(
                            probe.html_renderer,
                            "render_custom_template",
                            AsyncMock(return_value=str(source)),
                        ),
                    ]
                    if failure_stage == "copy":
                        patches.append(
                            patch.object(
                                probe.shutil,
                                "copyfile",
                                side_effect=OSError("copy failed"),
                            )
                        )
                    else:
                        patches.append(
                            patch.object(
                                probe,
                                "_inspect_image",
                                side_effect=RuntimeError("invalid image"),
                            )
                        )

                    with patches[0], patches[1], patches[2]:
                        with self.assertRaises((OSError, RuntimeError)):
                            await probe.run(root / "output")

                    self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
