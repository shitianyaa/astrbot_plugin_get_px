import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "checkin_card_v2"


class CheckinCardTemplateV2Test(unittest.TestCase):
    def test_required_regions_and_local_assets_exist(self):
        html = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
        css = (TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")

        for region in (
            "card-header",
            "identity",
            "greeting",
            "rewards",
            "growth",
            "artwork-credit",
        ):
            self.assertIn(f'class="{region}', html)

        self.assertIn('href="./style.css"', html)
        self.assertIn("width: 960px", css)
        self.assertIn("height: 540px", css)
        self.assertIsNone(re.search(r"https?://", html + css))


if __name__ == "__main__":
    unittest.main()
