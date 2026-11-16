import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "checkin_themes" / "default"


class CheckinCardTemplateTest(unittest.TestCase):
    def test_runtime_template_embeds_local_css(self):
        from astrbot_plugin_get_px.checkin.card import get_checkin_card_template

        template = get_checkin_card_template()
        self.assertNotIn("/*__CHECKIN_CARD_CSS__*/", template)
        self.assertNotIn('href="./style.css"', template)
        self.assertNotIn("__CHECKIN_CARD_FONT_DATA__", template)
        self.assertIn('url("data:font/woff2;base64,', template)
        self.assertIsNone(re.search(r"https?://", template))

    def test_bundled_fonts_have_valid_woff2_headers_and_size(self):
        fonts = list((TEMPLATE_DIR / "fonts").glob("*.woff2"))
        self.assertTrue(fonts)
        for font in fonts:
            with self.subTest(font=font.name):
                data = font.read_bytes()
                self.assertEqual(data[:4], b"wOF2")
                self.assertEqual(int.from_bytes(data[8:12], "big"), len(data))
                self.assertLess(len(data), 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
