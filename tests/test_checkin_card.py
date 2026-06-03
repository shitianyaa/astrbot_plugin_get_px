import base64
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin_card import (  # noqa: E402
    CHECKIN_CARD_HEIGHT,
    CHECKIN_CARD_WIDTH,
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
