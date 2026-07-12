import base64
from dataclasses import FrozenInstanceError
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px import checkin_card  # noqa: E402
from astrbot_plugin_get_px.checkin import CheckinProfile, CheckinRecord  # noqa: E402
from astrbot_plugin_get_px.checkin_card import (  # noqa: E402
    CardBackground,
    _file_to_data_url,
)
from astrbot_plugin_get_px.checkin_cache import CheckinCardCache  # noqa: E402
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
        self.assertEqual(view_model.milestone_next_text, "累计签到 30 天，还差 18 天")
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
    async def test_configured_jpeg_quality_is_passed_to_renderer(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.config = {
            "checkin_avatar_enabled": False,
            "checkin_card_quality": 97,
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
        self.assertEqual(options["quality"], 97)

    def test_quality_changes_the_daily_cache_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.config = {
                "checkin_avatar_enabled": False,
                "checkin_card_quality": 80,
            }
            plugin.checkin_cache = CheckinCardCache(Path(tmp) / "cache")
            kwargs = {
                "profile": _profile(),
                "record": _record(),
                "background": None,
                "bot_name": "neko",
            }

            quality_80 = plugin._checkin_card_cache_key(SimpleNamespace(), **kwargs)
            plugin.config["checkin_card_quality"] = 98
            quality_98 = plugin._checkin_card_cache_key(SimpleNamespace(), **kwargs)

            self.assertNotEqual(quality_80, quality_98)


if __name__ == "__main__":
    unittest.main()
