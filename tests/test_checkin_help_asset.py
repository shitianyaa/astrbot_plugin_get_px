"""Regression tests for the T2I-generated checkin help image."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px import main


def test_help_template_inlines_font_and_commands():
    from astrbot_plugin_get_px.templates.checkin_help.help_template import _help_template

    html = _help_template()
    assert "data:font/woff2;base64," in html  # 字体内联，无外部 URL
    assert not re.search(r"https?://", html)
    for cmd in (
        "签到日历",
        "签到状态",
        "签到成就",
        "签到生日",
        "签到称号",
        "签到排行",
        "签到商店",
        "签到主题",
        "签到管理",
    ):
        assert cmd in html


def test_help_image_is_readable_1440_wide_rgb():
    from PIL import Image

    with Image.open(main.CHECKIN_HELP_IMAGE) as img:
        assert img.size[0] == 1440
        assert img.mode == "RGB"
        img.verify()