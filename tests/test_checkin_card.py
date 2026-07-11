import base64
from dataclasses import FrozenInstanceError
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px import checkin_card  # noqa: E402
from astrbot_plugin_get_px.checkin import CheckinProfile, CheckinRecord  # noqa: E402
from astrbot_plugin_get_px.checkin_card import (  # noqa: E402
    CHECKIN_CARD_HEIGHT,
    CHECKIN_CARD_WIDTH,
    CardBackground,
    _file_to_data_url,
)

DATA_URL_PREFIX = "data:image/jpeg;base64,"


def _decode(url: str) -> Image.Image:
    assert url.startswith(DATA_URL_PREFIX), f"非预期返回: {url[:48]!r}"
    raw = base64.b64decode(url[len(DATA_URL_PREFIX):])
    return Image.open(BytesIO(raw)).convert("RGB")


def _make_image(tmp: str, name: str, size, color=(120, 80, 200)) -> str:
    path = Path(tmp) / name
    Image.new("RGB", size, color).save(path)
    return str(path)


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
        self.assertLessEqual(len(view_model.badges), 2)
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
    def test_various_sizes_normalize_to_card_dimensions(self):
        # 横图、竖图、小图、方图都应归一化到卡片尺寸 960x540
        cases = {
            "landscape.jpg": (1920, 1080),
            "portrait.jpg": (1200, 1700),
            "small.jpg": (400, 300),
            "square.png": (800, 800),
        }
        with tempfile.TemporaryDirectory() as tmp:
            for name, size in cases.items():
                out = _decode(_file_to_data_url(_make_image(tmp, name, size)))
                self.assertEqual(
                    out.size,
                    (CHECKIN_CARD_WIDTH, CHECKIN_CARD_HEIGHT),
                    f"{name} {size} 未归一化到卡片尺寸",
                )

    def test_extreme_aspect_ratio_covers_without_black_padding(self):
        # 极端宽图(10:1)：等比覆盖裁剪不应像旧实现那样把内容压扁 / 留黑边填充
        with tempfile.TemporaryDirectory() as tmp:
            src = _make_image(tmp, "wide.jpg", (2000, 200), color=(0, 200, 0))
            out = _decode(_file_to_data_url(src))
            pixels = list(out.getdata())
            black = sum(1 for r, g, b in pixels if r < 12 and g < 12 and b < 12)
            ratio = black / len(pixels)
            self.assertLess(ratio, 0.02, f"输出含 {ratio:.0%} 黑边，疑似非等比裁剪")

    def test_invalid_inputs_return_empty(self):
        self.assertEqual(_file_to_data_url(""), "")
        self.assertEqual(_file_to_data_url("/no/such/file.jpg"), "")
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "broken.jpg"
            broken.write_bytes(b"not an image")
            self.assertEqual(_file_to_data_url(str(broken)), "")


if __name__ == "__main__":
    unittest.main()
