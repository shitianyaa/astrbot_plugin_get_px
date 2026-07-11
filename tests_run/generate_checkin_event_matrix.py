from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Template
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT.parent))

from astrbot_plugin_get_px.checkin import CheckinProfile, CheckinRecord
from astrbot_plugin_get_px.checkin_card import (
    CHECKIN_CARD_TEMPLATE,
    CardBackground,
    build_checkin_card_data,
)
from astrbot_plugin_get_px.checkin_content import resolve_checkin_content
from astrbot_plugin_get_px.holiday_calendar import OnlineHoliday


OUTPUT_DIR = PROJECT_ROOT / "docs" / "images" / "checkin-event-matrix"


@dataclass(frozen=True)
class Scenario:
    slug: str
    label: str
    date_key: str = "2026-07-11"
    total_days: int = 12
    streak_days: int = 3
    affection: float = 35.0
    boost: bool = False
    birthday: str = ""
    custom_event: str = ""
    secondary_events: tuple[str, ...] = ()
    online_holiday: OnlineHoliday | None = None
    username: str = "测试用户"
    greeting_override: str = ""
    with_artwork: bool = True


SCENARIOS = (
    Scenario("01-normal", "普通签到"),
    Scenario("02-online-holiday", "联网法定节假日", online_holiday=OnlineHoliday("国庆黄金周", True)),
    Scenario("03-lunar-festival", "农历节日（春节）", date_key="2026-02-17"),
    Scenario("04-birthday", "生日" , birthday="生日"),
    Scenario("05-custom-event", "自定义纪念日", custom_event="相遇纪念日"),
    Scenario("06-milestone", "累计签到里程碑", total_days=30),
    Scenario("07-streak", "连续签到事件", total_days=31, streak_days=7),
    Scenario("08-boost", "双倍加持", boost=True),
    Scenario(
        "09-birthday-combo",
        "生日 + 30天 + 连签 + 加持",
        total_days=30,
        streak_days=7,
        boost=True,
        birthday="生日",
        affection=80.0,
    ),
    Scenario(
        "10-holiday-combo",
        "节假日 + 30天 + 连签",
        date_key="2026-10-01",
        total_days=30,
        streak_days=7,
    ),
    Scenario(
        "11-custom-combo",
        "纪念日 + 100天 + 加持",
        total_days=100,
        streak_days=14,
        boost=True,
        custom_event="第一次相遇纪念日",
    ),
    Scenario(
        "12-edge-content",
        "长昵称 + 最长寄语 + 无作品",
        username="一位昵称很长但仍应安全显示的访客",
        greeting_override="愿今天收藏的每一份温柔都在未来某个时刻重新照亮你，我们也会在下一页继续相遇。",
        with_artwork=False,
    ),
    Scenario(
        "13-workday",
        "调休工作日提示",
        online_holiday=OnlineHoliday("调休", False),
    ),
    Scenario(
        "14-event-collision",
        "生日 + 单次事件 + 年度事件 + 节日",
        date_key="2026-10-01",
        birthday="生日",
        custom_event="服务器特别活动",
        secondary_events=("年度相遇纪念日",),
        total_days=30,
        streak_days=7,
    ),
)


def make_profile(scenario: Scenario) -> CheckinProfile:
    return CheckinProfile(
        user_id="10001",
        coins=321,
        affection=scenario.affection,
        total_days=scenario.total_days,
        streak_days=scenario.streak_days,
        last_checkin_date=scenario.date_key,
        boost_start_date=scenario.date_key if scenario.boost else "",
        boost_until_date=scenario.date_key if scenario.boost else "",
        repeat_penalty_date="",
        repeat_penalty_total=0.0,
        created_at="2026-01-01T08:00:00+08:00",
        updated_at="2026-07-11T08:00:00+08:00",
    )


def make_record(scenario: Scenario) -> CheckinRecord:
    return CheckinRecord(
        date_key=scenario.date_key,
        user_id="10001",
        username=scenario.username,
        bot_name="Neko",
        base_coins=80,
        bonus_coins=20,
        coins_reward=100,
        base_affection=0.6,
        bonus_affection=0.4,
        affection_reward=1.0,
        boost_active=scenario.boost,
        boost_multiplier=2.0 if scenario.boost else 1.0,
        total_coins_after=321,
        total_affection_after=scenario.affection,
        total_days_after=scenario.total_days,
        streak_days_after=scenario.streak_days,
        note="",
        background_mode="pixiv_daily" if scenario.with_artwork else "fallback",
        background_source="visual-matrix",
        background_illust_id="445566" if scenario.with_artwork else "",
        background_title="夏日画页" if scenario.with_artwork else "",
        background_author="测试画师" if scenario.with_artwork else "",
        created_at="2026-07-11T08:00:00+08:00",
        updated_at="2026-07-11T08:00:00+08:00",
    )


def make_artwork() -> Path:
    path = OUTPUT_DIR / "_fixture-artwork.png"
    image = Image.new("RGB", (750, 1000), "#d9b9a4")
    draw = ImageDraw.Draw(image)
    for y in range(1000):
        color = (217 - y // 20, 185 - y // 35, 164 + y // 25)
        draw.line((0, y, 750, y), fill=tuple(max(0, min(255, c)) for c in color))
    draw.ellipse((120, 170, 630, 680), fill="#f3dfcb", outline="#8e5a4a", width=8)
    draw.line((160, 760, 590, 760), fill="#8e5a4a", width=5)
    image.save(path)
    return path


async def render_matrix() -> list[dict[str, object]]:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    artwork_path = make_artwork()
    report: list[dict[str, object]] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 960, "height": 540})
        page = await context.new_page()

        for scenario in SCENARIOS:
            profile = make_profile(scenario)
            record = make_record(scenario)
            content = resolve_checkin_content(
                record,
                profile,
                birthday_label=scenario.birthday,
                custom_event_label=scenario.custom_event,
                online_holiday=scenario.online_holiday,
                secondary_event_labels=scenario.secondary_events,
                current_title="七日同行",
                unlocked_achievements=("月下常客",),
            )
            if scenario.greeting_override:
                content = content.__class__(
                    title=content.title,
                    event_key=content.event_key,
                    event_label=content.event_label,
                    greeting=scenario.greeting_override,
                    badges=content.badges,
                    secondary_note=content.secondary_note,
                    context=content.context,
                )
            background = CardBackground(
                image_path=str(artwork_path) if scenario.with_artwork else "",
                illust_id="445566" if scenario.with_artwork else "",
                title="夏日画页" if scenario.with_artwork else "",
                author="测试画师" if scenario.with_artwork else "",
                illust={"width": 750, "height": 1000} if scenario.with_artwork else None,
            )
            data = build_checkin_card_data(
                profile=profile,
                record=record,
                bot_name="Neko",
                background=background,
                user_title="今日旅人",
                content=content,
            )
            html = Template(CHECKIN_CARD_TEMPLATE).render(data)
            html_path = OUTPUT_DIR / f"{scenario.slug}.html"
            html_path.write_text(html, encoding="utf-8")
            await page.goto(html_path.resolve().as_uri())
            await page.wait_for_timeout(100)

            diagnostics = await page.evaluate(
                """() => {
                    const stage = document.querySelector('.stage').getBoundingClientRect();
                    const overflowing = [...document.querySelectorAll('.checkin-card *')]
                      .filter((el) => el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)
                      .map((el) => el.className || el.tagName);
                    const outside = [...document.querySelectorAll('.checkin-card *')]
                      .filter((el) => {
                        const r = el.getBoundingClientRect();
                        return r.left < stage.left - 1 || r.top < stage.top - 1 || r.right > stage.right + 1 || r.bottom > stage.bottom + 1;
                      })
                      .map((el) => el.className || el.tagName);
                    return { overflowing, outside };
                }"""
            )
            png_path = OUTPUT_DIR / f"{scenario.slug}.png"
            await page.locator(".stage").screenshot(path=str(png_path))
            with Image.open(png_path) as image:
                size = image.size
            report.append(
                {
                    "scenario": scenario.slug,
                    "label": scenario.label,
                    "event_key": content.event_key,
                    "size": list(size),
                    **diagnostics,
                }
            )
            html_path.unlink(missing_ok=True)

        await browser.close()

    artwork_path.unlink(missing_ok=True)
    return report


def build_contact_sheet(report: list[dict[str, object]]) -> Path:
    thumb_size = (480, 270)
    cell_size = (500, 310)
    columns = 2
    rows = (len(report) + columns - 1) // columns
    sheet = Image.new("RGB", (cell_size[0] * columns, cell_size[1] * rows), "#eee9df")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 18)
    for index, item in enumerate(report):
        x = (index % columns) * cell_size[0] + 10
        y = (index // columns) * cell_size[1] + 8
        with Image.open(OUTPUT_DIR / f"{item['scenario']}.png") as source:
            thumb = source.resize(thumb_size, Image.Resampling.LANCZOS)
        sheet.paste(thumb, (x, y + 28))
        status = "PASS" if not item["outside"] else "OUTSIDE"
        draw.text((x, y), f"{item['scenario']}  {item['label']}  [{status}]", fill="#332b26", font=font)
    path = OUTPUT_DIR / "checkin-event-matrix-contact-sheet.png"
    sheet.save(path)
    return path


async def main() -> None:
    report = await render_matrix()
    report_path = OUTPUT_DIR / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    contact_sheet = build_contact_sheet(report)
    failures = [item for item in report if item["size"] != [960, 540] or item["outside"]]
    print(f"Rendered {len(report)} scenarios: {contact_sheet}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(f"Visual matrix has {len(failures)} failing scenarios")


if __name__ == "__main__":
    asyncio.run(main())
