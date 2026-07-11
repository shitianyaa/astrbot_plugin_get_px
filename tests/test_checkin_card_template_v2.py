import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "checkin_card_v2"


def _css_rule(css: str, selector: str) -> str:
    match = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css, re.S)
    assert match is not None, f"missing CSS selector: {selector}"
    return match.group("body")


class CheckinCardTemplateV2Test(unittest.TestCase):
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
        from astrbot_plugin_get_px.checkin_card import CHECKIN_CARD_TEMPLATE

        self.assertNotIn("/*__CHECKIN_CARD_CSS__*/", CHECKIN_CARD_TEMPLATE)
        self.assertNotIn('href="./style.css"', CHECKIN_CARD_TEMPLATE)
        self.assertIn(".paper-sheet", CHECKIN_CARD_TEMPLATE)
        self.assertIsNone(re.search(r"https?://", CHECKIN_CARD_TEMPLATE))

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
        for match in re.finditer(r"(?P<selector>[^{}]+)\{(?P<body>[^{}]*)\}", css, re.S):
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

        self.assertIn("gap: 4px", information)
        self.assertIn("display: flex", heading)
        self.assertIn("align-items: baseline", heading)
        self.assertIn("padding-block: 10px", artwork_column)
        self.assertIn("width: 306px", artwork_frame)
        self.assertIn("height: 408px", artwork_frame)


if __name__ == "__main__":
    unittest.main()
