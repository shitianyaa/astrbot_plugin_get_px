"""帮助图资源自包含与可解码性。"""

from pathlib import Path
import sys

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px import main
from astrbot_plugin_get_px.templates.checkin_help.help_template import _help_template


def test_help_assets_are_self_contained_and_decodable():
    html = _help_template()
    assert "data:font/woff2;base64," in html
    assert "https://" not in html and "http://" not in html
    with Image.open(main.CHECKIN_HELP_IMAGE) as image:
        assert image.format == "PNG"
        assert image.width > 0 and image.height > 0
        image.verify()
