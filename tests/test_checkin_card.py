import base64
import os
from dataclasses import FrozenInstanceError, replace
import random
import struct
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image, ImageCms

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
    def test_prevalidated_background_reuses_encoding(self):
        background = CardBackground(
            image_path="already-validated.png", mode="pixiv_daily",
            illust_id="123", data_url="data:image/png;base64,fixture",
        )
        with patch.object(checkin_card, "_file_to_data_url") as encode:
            data = checkin_card.build_checkin_card_data(
                profile=_profile(), record=_record(), bot_name="neko", background=background,
            )
        self.assertEqual(data["artwork_url"], background.data_url)
        encode.assert_not_called()

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

    def test_oversized_artwork_is_downscaled_and_reencoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "large.png"
            Image.new("RGB", (2400, 3200), (20, 90, 180)).save(
                source, format="PNG"
            )

            data_url = _file_to_data_url(str(source))
            header, encoded = data_url.split(",", 1)
            decoded = base64.b64decode(encoded)

            self.assertEqual(header, "data:image/jpeg;base64")
            with Image.open(BytesIO(decoded)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertLessEqual(max(image.size), 1920)
                self.assertGreaterEqual(max(image.size), 1900)
                ratio = image.size[0] / image.size[1]
                self.assertAlmostEqual(ratio, 2400 / 3200, places=2)

    def test_heavy_artwork_within_edge_limit_is_recompressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "heavy.png"
            payload = os.urandom(1600 * 1200 * 3)
            Image.frombytes("RGB", (1600, 1200), payload).save(source, format="PNG")
            self.assertGreater(source.stat().st_size, 1_500_000)

            data_url = _file_to_data_url(str(source))
            header, encoded = data_url.split(",", 1)
            decoded = base64.b64decode(encoded)

            self.assertEqual(header, "data:image/jpeg;base64")
            with Image.open(BytesIO(decoded)) as image:
                self.assertEqual(image.format, "JPEG")
                self.assertEqual(image.size, (1600, 1200))

    def test_exif_orientation_is_applied_when_reencoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "rotated.jpg"
            exif = Image.Exif()
            exif[0x0112] = 6  # 顺时针 90°，显示时宽高互换
            Image.new("RGB", (2400, 1200), (30, 60, 90)).save(
                source, format="JPEG", exif=exif
            )

            data_url = _file_to_data_url(str(source))
            _, encoded = data_url.split(",", 1)
            with Image.open(BytesIO(base64.b64decode(encoded))) as image:
                self.assertEqual(image.size, (960, 1920))

    def test_rgba_artwork_keeps_alpha_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "alpha.png"
            image = Image.new("RGBA", (2400, 1600), (0, 0, 0, 0))
            image.paste((200, 40, 40, 255), (200, 200, 2200, 1400))
            image.save(source, format="PNG")

            header, encoded = _file_to_data_url(str(source)).split(",", 1)
            self.assertEqual(header, "data:image/png;base64")
            with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
                self.assertEqual(decoded.format, "PNG")
                self.assertEqual(decoded.mode, "RGBA")
                self.assertEqual(decoded.getpixel((0, 0))[3], 0)
                self.assertEqual(decoded.getpixel((1200, 800))[3], 255)

    def test_rgb_transparency_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "rgb-transparent.png"
            Image.new("RGB", (2400, 1600), (255, 0, 0)).save(
                source, format="PNG", transparency=(255, 0, 0)
            )

            header, encoded = _file_to_data_url(str(source)).split(",", 1)
            self.assertEqual(header, "data:image/png;base64")
            with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
                self.assertEqual(decoded.mode, "RGBA")
                self.assertEqual(decoded.getpixel((0, 0))[3], 0)

    def test_grayscale_transparency_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "gray-transparent.png"
            Image.new("L", (2400, 1600), 0).save(
                source, format="PNG", transparency=0
            )

            header, encoded = _file_to_data_url(str(source)).split(",", 1)
            self.assertEqual(header, "data:image/png;base64")
            with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
                self.assertEqual(decoded.mode, "RGBA")
                self.assertEqual(decoded.getpixel((0, 0))[3], 0)

    def test_calendar_background_keeps_ultra_edge(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "calendar.png"
            Image.new("RGB", (4000, 2250), (10, 20, 30)).save(
                source, format="PNG"
            )

            _, encoded = _file_to_data_url(
                str(source), max_edge=2880
            ).split(",", 1)
            with Image.open(BytesIO(base64.b64decode(encoded))) as image:
                self.assertEqual(image.size, (2880, 1620))

    def test_color_key_transparency_survives_resampling(self):
        for mode, row, key in (
            ("RGB", b"\xff\x00\x00\x00\x00\xff" * 32, (255, 0, 0)),
            ("L", b"\x00\xff" * 32, 0),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "color-key.png"
                Image.frombytes(mode, (64, 64), row * 64).save(
                    source, transparency=key
                )
                _, encoded = _file_to_data_url(str(source), max_edge=32).split(",", 1)
                with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
                    alpha_min, alpha_max = decoded.getchannel("A").getextrema()
                    self.assertGreater(alpha_min, 0)
                    self.assertLess(alpha_max, 255)

    def test_reencoded_artwork_fits_byte_budget(self):
        rng = random.Random(22)
        for mode, size, max_edge in (
            ("RGB", (1920, 1920), 1920),
            ("RGBA", (1920, 1280), 1920),
            ("RGBA", (2880, 1620), 2880),
        ):
            with self.subTest(mode=mode, size=size), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "detailed.png"
                Image.frombytes(
                    mode, size, rng.randbytes(size[0] * size[1] * len(mode))
                ).save(source)
                original = source.read_bytes()
                _, encoded = _file_to_data_url(
                    str(source), max_edge=max_edge
                ).split(",", 1)
                payload = base64.b64decode(encoded)
                self.assertLessEqual(len(payload), 1_500_000)
                self.assertLessEqual(len(encoded), 2_000_000)
                with Image.open(BytesIO(payload)) as decoded:
                    self.assertLess(max(decoded.size), max_edge)
                    self.assertAlmostEqual(
                        decoded.width / decoded.height, size[0] / size[1], places=2
                    )
                    if mode == "RGBA":
                        self.assertEqual(decoded.mode, "RGBA")
                        self.assertLess(decoded.getchannel("A").getextrema()[1], 255)
                self.assertEqual(source.read_bytes(), original)

    def test_transparent_webp_does_not_expand_past_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "compact.webp"
            size = (2400, 1600)
            image = Image.frombytes(
                "RGB", size, random.Random(22).randbytes(size[0] * size[1] * 3)
            )
            image.putalpha(128)
            image.save(source, format="WEBP", quality=10, method=2)
            _, encoded = _file_to_data_url(str(source)).split(",", 1)
            payload = base64.b64decode(encoded)
            self.assertLessEqual(len(payload), 1_500_000)
            with Image.open(BytesIO(payload)) as decoded:
                self.assertEqual(decoded.format, "PNG")
                self.assertEqual(decoded.getchannel("A").getextrema(), (128, 128))

    def test_pixel_budget_rejects_artwork_before_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "over-budget.png"
            Image.new("1", (8000, 5001), 1).save(source)
            with patch.object(
                Image.Image, "load", side_effect=AssertionError("unexpected decode")
            ) as load:
                self.assertEqual(_file_to_data_url(str(source)), "")
                load.assert_not_called()

    def test_icc_colors_are_converted_to_srgb_with_alpha_preserved(self):
        # 将 sRGB 的传递曲线改成线性，构造像素值相同但显示颜色不同的有效 RGB 配置。
        profile = bytearray(
            ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        )
        for index in range(struct.unpack_from(">I", profile, 128)[0]):
            signature, offset, _size = struct.unpack_from(">4sII", profile, 132 + index * 12)
            if signature in (b"rTRC", b"gTRC", b"bTRC"):
                self.assertEqual(profile[offset : offset + 4], b"para")
                struct.pack_into(">H", profile, offset + 8, 0)
                struct.pack_into(">i", profile, offset + 12, 65536)
        profile[84:100] = bytes(16)
        source_profile = ImageCms.ImageCmsProfile(BytesIO(bytes(profile)))
        srgb = ImageCms.createProfile("sRGB")
        reference = ImageCms.profileToProfile(
            Image.new("RGB", (1, 1), (64, 128, 192)),
            source_profile,
            srgb,
            outputMode="RGB",
        ).getpixel((0, 0))
        self.assertGreater(reference[0], 100)
        for mode in ("RGB", "RGBA"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                source = Path(tmp) / "profiled.png"
                color = (64, 128, 192, 100) if mode == "RGBA" else (64, 128, 192)
                Image.new(mode, (64, 64), color).save(source, icc_profile=bytes(profile))
                _, encoded = _file_to_data_url(str(source), max_edge=32).split(",", 1)
                with Image.open(BytesIO(base64.b64decode(encoded))) as decoded:
                    actual = decoded.convert("RGB").getpixel((0, 0))
                    for channel, expected in zip(actual, reference):
                        self.assertLessEqual(abs(channel - expected), 2)
                    if mode == "RGBA":
                        self.assertEqual(decoded.getchannel("A").getextrema(), (100, 100))

    def test_invalid_icc_uses_existing_background_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invalid-profile.png"
            Image.new("RGB", (64, 64)).save(source, icc_profile=b"invalid ICC profile")
            self.assertEqual(_file_to_data_url(str(source), max_edge=32), "")


class CheckinCardRenderQualityTest(unittest.IsolatedAsyncioTestCase):
    async def test_configured_jpeg_quality_is_passed_to_renderer(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {
            "checkin_avatar_enabled": False,
            "checkin_background_refresh_cost": 100,
            "checkin_card_quality_tier": "清晰",
        }
        plugin.html_render = AsyncMock(return_value="card.jpg")

        result = await plugin._render_checkin_card(
            SimpleNamespace(),
            profile=_profile(),
            record=_record(),
            background=None,
            bot_name="neko",
        )

        self.assertEqual(result, "card.jpg")
        options = plugin.html_render.await_args.kwargs["options"]
        self.assertEqual(options["type"], "jpeg")
        self.assertEqual(options["quality"], 95)
        self.assertEqual(options["device_scale_factor_level"], "high")

    def test_render_tier_changes_the_daily_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "checkin_avatar_enabled": False,
                "checkin_background_refresh_cost": 100,
                "checkin_card_quality_tier": "省流量",
            }
            plugin.checkin_cache = CheckinCardCache(Path(tmp) / "cache")
            kwargs = {
                "profile": _profile(),
                "record": _record(),
                "background": None,
                "bot_name": "neko",
            }

            quality_80 = plugin._checkin_card_cache_key(
                SimpleNamespace(), render_tier="省流量", **kwargs
            )
            quality_98 = plugin._checkin_card_cache_key(
                SimpleNamespace(), render_tier="极致", **kwargs
            )

            self.assertNotEqual(quality_80, quality_98)

    def test_current_theme_version_invalidates_an_older_daily_cache(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {
            "checkin_avatar_enabled": False,
            "checkin_card_quality": 95,
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
        self.assertEqual(
            plugin.checkin_cache.cache_key.call_args.kwargs["view_model"]["resource_version"],
            3,
        )


class CheckinInlineResourcesTest(unittest.IsolatedAsyncioTestCase):
    def plugin(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {"checkin_avatar_enabled": True}
        plugin.downloader = SimpleNamespace(download=AsyncMock())
        plugin.html_render = AsyncMock(return_value="unused.jpg")
        return plugin

    def event(self, platform="aiocqhttp"):
        return SimpleNamespace(
            get_sender_id=lambda: "10001",
            get_platform_name=lambda: platform,
        )

    def arguments(self):
        return dict(
            profile=_profile(), record=_record(), background=None, bot_name="neko"
        )

    async def test_avatar_is_inlined_in_every_theme_and_temp_file_removed(self):
        from html.parser import HTMLParser
        from jinja2 import Template
        from astrbot_plugin_get_px.checkin.themes import CHECKIN_THEMES

        class Resources(HTMLParser):
            def handle_starttag(self, tag, attrs):
                for key, value in attrs:
                    if key in ("src", "href") and value:
                        if value.startswith(("http://", "https://", "//")):
                            raise AssertionError(f"external resource in {tag}")

        plugin = self.plugin()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "avatar.png"
            Image.new("RGB", (64, 64), "red").save(source)
            plugin.downloader.download.return_value = (
                str(source),
                source.stat().st_size,
            )
            await plugin._render_checkin_card(self.event(), **self.arguments())
            self.assertFalse(source.exists())
        data = plugin.html_render.await_args.args[1]
        self.assertTrue(data["avatar_url"].startswith("data:image/png;base64,"))
        for theme_id in CHECKIN_THEMES:
            html = Template(checkin_card.get_checkin_card_template(theme_id)).render(
                **data
            )
            Resources().feed(html)
            self.assertNotRegex(html, r"url\([\s\"\x27]*(?:https?:)?//")

    async def test_avatar_failure_uses_placeholder_and_cleans_invalid_file(self):
        plugin = self.plugin()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invalid.jpg"
            source.write_bytes(b"not an image")
            plugin.downloader.download.return_value = (
                str(source),
                source.stat().st_size,
            )
            await plugin._render_checkin_card(self.event(), **self.arguments())
            self.assertFalse(source.exists())
        self.assertEqual(plugin.html_render.await_args.args[1]["avatar_url"], "")
        plugin.downloader.download.side_effect = TimeoutError()
        await plugin._render_checkin_card(self.event(), **self.arguments())
        self.assertEqual(plugin.html_render.await_args.args[1]["avatar_url"], "")

    async def test_disabled_and_other_platform_do_not_download(self):
        plugin = self.plugin()
        await plugin._render_checkin_card(self.event("other"), **self.arguments())
        plugin.config["checkin_avatar_enabled"] = False
        await plugin._render_checkin_card(self.event(), **self.arguments())
        plugin.downloader.download.assert_not_awaited()

    async def test_cancel_during_avatar_encoding_cleans_file(self):
        import asyncio
        from astrbot_plugin_get_px.checkin import artwork

        plugin = self.plugin()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "avatar.png"
            Image.new("RGB", (64, 64)).save(source)
            plugin.downloader.download.return_value = (
                str(source),
                source.stat().st_size,
            )
            with patch.object(
                artwork.asyncio,
                "to_thread",
                new=AsyncMock(side_effect=asyncio.CancelledError),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await plugin._render_checkin_card(self.event(), **self.arguments())
            self.assertFalse(source.exists())
        plugin.html_render.assert_not_awaited()

    async def test_fallback_prepares_resources_once(self):
        from astrbot_plugin_get_px.checkin import artwork

        plugin = self.plugin()
        plugin.html_render.side_effect = RuntimeError("T2I unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "avatar.png"
            Image.new("RGB", (64, 64)).save(source)
            plugin.downloader.download.return_value = (
                str(source),
                source.stat().st_size,
            )
            with patch.object(
                artwork,
                "build_checkin_card_data",
                wraps=artwork.build_checkin_card_data,
            ) as build:
                with self.assertRaisesRegex(RuntimeError, "all check-in"):
                    await plugin._render_checkin_card_with_fallback(
                        self.event(),
                        preferred_tier="极致",
                        **self.arguments(),
                    )
                self.assertEqual(build.call_count, 1)
        plugin.downloader.download.assert_awaited_once()
        self.assertEqual(plugin.html_render.await_count, 3)

    async def test_cache_hit_skips_resource_preparation(self):
        plugin = self.plugin()
        plugin._checkin_card_cache_key = Mock(return_value="key")
        cache = SimpleNamespace(store=AsyncMock(return_value="cached.jpg"))
        result, _tier = await plugin._render_checkin_card_with_fallback(
            self.event(),
            preferred_tier="极致",
            cache=cache,
            **self.arguments(),
        )
        self.assertEqual(result, Path("cached.jpg"))
        plugin.downloader.download.assert_not_awaited()
        plugin.html_render.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
