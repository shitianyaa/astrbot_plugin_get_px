"""Generate preview.html and preview.png with the shared real Pixiv artwork sample."""

import base64
from pathlib import Path
from jinja2 import Template
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
ARTWORK_PATH = ROOT.parent / "preview_assets" / "artwork_3x4.jpg"

# Read HTML and CSS
html_tpl = (ROOT / "index.html").read_text(encoding="utf-8")
css = (ROOT / "style.css").read_text(encoding="utf-8")

# Fix font path for browser preview
css = css.replace(
    'url("data:font/woff2;base64,__CHECKIN_CARD_FONT_DATA__") format("woff2")',
    'url("../../checkin_card_v2/fonts/LXGWWenKaiLite-GB2312.woff2") format("woff2")',
)

# Insert CSS into HTML
html_tpl = html_tpl.replace("/*__CHECKIN_CARD_CSS__*/", css)
artwork_data = base64.b64encode(ARTWORK_PATH.read_bytes()).decode("ascii")

# Sample Data for rendering
data = {
    "width": 960,
    "height": 540,
    "title": "生日纪念",
    "date_label": "2026-07-11 · 星期六",
    "badges": ("连签7天",),
    "avatar_url": "",
    "avatar_initial": "访",
    "username": "一位昵称很长但仍应安全显示的访客",
    "user_title": "早起的旅行者",
    "event_label": "七月生日",
    "greeting": "恋ではなく、愛でもなく、もっとずっと深く重い。",
    "secondary_note": "",
    "bot_name": "neko",
    "coins_reward": 100,
    "affection_reward_label": "1.00",
    "boost_multiplier": 2.0,
    "boost_multiplier_label": "2",
    "streak_days": 5,
    "artwork_url": f"data:image/jpeg;base64,{artwork_data}",
    "artwork_title": "いちごちゃん",
    "artwork_credit": "今日作品：いちごちゃん / 廉訳 · Pixiv 146891316",
    "total_days": 12,
    "coins_total": 321,
    "affection_value_label": "66.60",
    "affection_level": "亲近",
    "affection_next_text": "距离“信赖”还需 3.40",
    "affection_progress": 72,
    "milestone_next_text": "累计签到 30 天，还差 18 天",
    "theme_id": "03",
    "theme_name": "黄黑",
    "background_refresh_cost": 100,
}

# Render
rendered = Template(html_tpl).render(data)

out = ROOT / "preview.html"
out.write_text(rendered, encoding="utf-8")
print(f"wrote {out} ({out.stat().st_size} bytes)")

# Generate preview.png using Playwright
print("Generating preview.png using Playwright...")
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 960, "height": 540})
    page.goto(out.resolve().as_uri())
    page.wait_for_timeout(500)  # Wait for font rendering or animations
    page.locator(".stage").screenshot(path=str(ROOT / "preview.png"))
    browser.close()
print("preview.png updated successfully.")
