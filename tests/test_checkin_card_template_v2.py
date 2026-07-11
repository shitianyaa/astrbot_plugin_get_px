import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "checkin_card_v2"


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


if __name__ == "__main__":
    unittest.main()
