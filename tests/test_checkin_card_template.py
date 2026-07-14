import re
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "checkin_themes" / "default"


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css, re.S)
    assert match is not None, f"missing CSS selector: {selector}"
    return match.group("body")


class CheckinCardTemplateTest(unittest.TestCase):
    def test_static_checkin_help_image_is_bundled(self):
        from astrbot_plugin_get_px.main import CHECKIN_HELP_IMAGE, GetPxPlugin

        self.assertEqual(CHECKIN_HELP_IMAGE, ROOT / "assets" / "checkin_help.png")
        self.assertTrue(CHECKIN_HELP_IMAGE.is_file())
        with Image.open(CHECKIN_HELP_IMAGE) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (1440, 2040))
        self.assertTrue(hasattr(GetPxPlugin, "cmd_checkin_help"))
        self.assertTrue(hasattr(GetPxPlugin, "cmd_preview_checkin_theme"))

    def test_required_h_paper_album_regions_exist(self):
        html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")

        for region in (
            "paper-sheet",
            "information-column",
            "artwork-frame",
            "rewards",
            "account-summary",
            "affection-progress",
            "artwork-credit",
        ):
            self.assertIn(f'class="{region}', html)

        self.assertIn("/*__CHECKIN_CARD_CSS__*/", html)
        self.assertIn("width: 960px", css)
        self.assertIn("height: 540px", css)
        self.assertIn("object-fit: contain", css)
        self.assertIn("object-position: center", css)
        self.assertIsNone(re.search(r"https?://", html + css))

    def test_runtime_template_embeds_local_css(self):
        from astrbot_plugin_get_px.checkin.card import CHECKIN_CARD_TEMPLATE

        self.assertNotIn("/*__CHECKIN_CARD_CSS__*/", CHECKIN_CARD_TEMPLATE)
        self.assertNotIn('href="./style.css"', CHECKIN_CARD_TEMPLATE)
        self.assertNotIn("__CHECKIN_CARD_FONT_DATA__", CHECKIN_CARD_TEMPLATE)
        self.assertIn(".paper-sheet", CHECKIN_CARD_TEMPLATE)
        self.assertIn('url("data:font/woff2;base64,', CHECKIN_CARD_TEMPLATE)
        self.assertIsNone(re.search(r"https?://", CHECKIN_CARD_TEMPLATE))

    def test_local_font_asset_is_bundled_and_reasonably_sized(self):
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")
        font_path = TEMPLATE_DIR / "fonts" / "LXGWWenKaiLite-GB2312.woff2"

        self.assertTrue(font_path.is_file())
        self.assertLess(font_path.stat().st_size, 2 * 1024 * 1024)
        self.assertIn("LXGW WenKai Lite", css)
        self.assertIn("__CHECKIN_CARD_FONT_DATA__", css)
        self.assertNotIn("STKaiti", css)
        self.assertNotIn("KaiTi", css)

    def test_stage_contains_paper_inside_the_fixed_canvas(self):
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")

        stage = _css_rule(css, ".stage")
        paper = _css_rule(css, ".checkin-card")
        self.assertIn("width: 960px", stage)
        self.assertIn("height: 540px", stage)
        self.assertIn("padding: 24px", stage)
        self.assertIn("box-sizing: border-box", stage)
        self.assertIn("width: 100%", paper)
        self.assertIn("height: 100%", paper)

    def test_only_artwork_credit_may_use_text_smaller_than_18px(self):
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")
        undersized: list[tuple[str, int]] = []
        for match in re.finditer(
            r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}", css, re.S
        ):
            selector = match.group("selector").strip()
            for size in re.findall(r"font-size:\s*(\d+)px", match.group("body")):
                pixels = int(size)
                if pixels < 18 and ".artwork-credit" not in selector:
                    undersized.append((selector, pixels))

        self.assertEqual(undersized, [])

    def test_maximum_state_reserves_vertical_safe_space(self):
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")

        information = _css_rule(css, ".information-column")
        heading = _css_rule(css, ".heading-copy")
        artwork_column = _css_rule(css, ".artwork-column")
        artwork_frame = _css_rule(css, ".artwork-frame")

        self.assertIn("justify-content: space-between", information)
        self.assertIn("display: flex", heading)
        self.assertIn("align-items: flex-start", heading)
        self.assertIn("flex-direction: column", heading)
        self.assertIn("padding-block: 10px", artwork_column)
        self.assertIn("width: 306px", artwork_frame)
        self.assertIn("height: 408px", artwork_frame)

    def test_compact_layout_removes_repeated_status_fields(self):
        html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")

        self.assertNotIn("<span>关系等级</span>", html)
        self.assertNotIn('class="boost-status"', html)
        self.assertEqual(html.count('class="badge"'), 1)

        summary = _css_rule(css, ".account-summary")
        nickname = _css_rule(css, ".identity-copy strong")
        nickname_only = _css_rule(css, ".identity-copy strong:only-child")
        identity = _css_rule(css, ".identity-copy")
        signature = _css_rule(css, ".greeting .signature")
        self.assertIn("repeat(2", summary)
        self.assertIn("white-space: nowrap", nickname)
        self.assertIn("text-overflow: ellipsis", nickname)
        self.assertIn("max-width: 100%", nickname_only)
        self.assertIn("flex: 1 1 0", identity)
        self.assertIn("text-align: right", signature)
        self.assertIn(
            ".milestone-line {\n  justify-content: flex-start;\n}",
            css,
        )
        self.assertIn("下一成就", html)


if __name__ == "__main__":
    unittest.main()
