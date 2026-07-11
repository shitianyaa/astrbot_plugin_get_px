from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
from datetime import date
from html import escape
from pathlib import Path
from typing import Any, Iterable

from astrbot.api import logger

from .checkin import (
    CheckinProfile,
    CheckinRecord,
    affection_level,
    boost_remaining_days,
)
from .checkin_content import CheckinContent, MILESTONES


CHECKIN_CARD_WIDTH = 960
CHECKIN_CARD_HEIGHT = 540
CHECKIN_CARD_JPEG_QUALITY = 85

HEADSHOT_CROP_TOP_RATIO = 0.3  # Task 4 will replace legacy full-card normalization.

_TEMPLATE_DIR = Path(__file__).with_name("templates") / "checkin_card_v2"
_CSS_MARKER = "/*__CHECKIN_CARD_CSS__*/"


def _load_checkin_card_template() -> str:
    html = (_TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")
    css = (_TEMPLATE_DIR / "style.css").read_text(encoding="utf-8")
    if _CSS_MARKER not in html:
        raise RuntimeError("check-in card template is missing its CSS marker")
    return html.replace(_CSS_MARKER, css)


CHECKIN_CARD_TEMPLATE = _load_checkin_card_template()


@dataclass(frozen=True)
class CardBackground:
    image_path: str = ""
    mode: str = ""
    source: str = ""
    illust_id: str = ""
    title: str = ""
    author: str = ""
    illust: dict[str, Any] | None = None
    quality: str = ""
    file_size: int = 0

    @property
    def pixiv_caption(self) -> str:
        if not self.illust_id:
            return ""
        label = self.title or "无标题"
        if self.author:
            label = f"{label} / {self.author}"
        return f"背景：{label} (ID: {self.illust_id})"


@dataclass(frozen=True)
class CheckinCardViewModel:
    date_label: str
    title: str
    badges: tuple[str, ...]
    event_label: str
    username: str
    avatar_url: str
    user_title: str
    bot_name: str
    greeting: str
    greeting_source: str
    secondary_note: str
    coins_reward: int
    affection_reward: float
    boost_multiplier: float
    boost_status_text: str
    streak_days: int
    total_days: int
    coins_total: int
    affection_value: float
    affection_level: str
    affection_next_text: str
    milestone_next_text: str
    artwork_title: str
    artwork_author: str
    artwork_id: str
    artwork_url: str
    artwork_aspect_ratio: float | None


_NEXT_AFFECTION_LEVEL = {
    "排斥": "陌生",
    "陌生": "熟悉",
    "熟悉": "亲近",
    "亲近": "信赖",
    "信赖": "挚友",
}


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _truncate_display(value: object, limit: int) -> str:
    text = _one_line(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _date_label(date_key: str) -> str:
    weekdays = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
    try:
        day = date.fromisoformat(date_key)
    except ValueError:
        return _one_line(date_key)
    return f"{date_key} · {weekdays[day.weekday()]}"


def _card_title(event_key: str, event_label: str) -> str:
    if event_key == "birthday":
        return "生日纪念"
    if event_key == "custom":
        return "纪念日相遇"
    if event_key == "milestone":
        return "签到纪念"
    if event_key == "streak":
        return "连续相遇"
    if event_label:
        return _truncate_display(event_label.removesuffix("节") + "相遇", 12)
    return "今日签到"


def _default_badges(record: CheckinRecord) -> tuple[str, ...]:
    badges: list[str] = []
    if record.event_key == "birthday":
        badges.append("生日")
    elif record.event_label:
        badges.append(_truncate_display(record.event_label, 8))

    if record.total_days_after in MILESTONES and record.event_key != "milestone":
        badges.append(f"{record.total_days_after}天")
    elif (
        record.streak_days_after > 0
        and record.streak_days_after % 7 == 0
        and record.event_key != "streak"
    ):
        badges.append(f"连签{record.streak_days_after}天")
    if record.boost_active:
        badges.append(f"×{record.boost_multiplier:g}")
    return tuple(badges[:2])


def _affection_next_text(value: float, level: dict[str, Any]) -> str:
    next_value = level["next"]
    if next_value is None:
        return "已达最高关系等级"
    next_name = _NEXT_AFFECTION_LEVEL.get(str(level["name"]), "下一等级")
    return f"距离“{next_name}”还需 {max(0.0, float(next_value) - value):.2f}"


def _milestone_next_text(total_days: int) -> str:
    for milestone in MILESTONES:
        if milestone > total_days:
            return f"累计签到 {milestone} 天，还差 {milestone - total_days} 天"
    return "已完成全部签到纪念"


def _artwork_aspect_ratio(background: CardBackground) -> float | None:
    illust = background.illust or {}
    try:
        width = float(illust.get("width") or 0)
        height = float(illust.get("height") or 0)
        if width > 0 and height > 0:
            return width / height
    except (TypeError, ValueError):
        pass
    return None


def build_checkin_card_view_model(
    *,
    profile: CheckinProfile,
    record: CheckinRecord,
    bot_name: str,
    avatar_url: str = "",
    background: CardBackground | None = None,
    user_title: str = "",
    content: CheckinContent | None = None,
) -> CheckinCardViewModel:
    background = background or CardBackground()
    event_label = content.event_label if content else record.event_label
    greeting = content.greeting if content else record.greeting
    secondary_note = content.secondary_note if content else record.secondary_note
    title = content.title if content else _card_title(record.event_key, event_label)
    badges: Iterable[str] = content.badges if content else _default_badges(record)

    snapshot_affection = float(record.total_affection_after)
    level = affection_level(snapshot_affection)
    remaining = boost_remaining_days(profile, record.date_key)
    boost_status = ""
    if record.boost_active:
        boost_status = (
            f"加持剩余 {remaining} 天"
            if remaining > 0
            else f"好感度奖励 ×{record.boost_multiplier:g}"
        )

    artwork_title = background.title or record.background_title
    artwork_author = background.author or record.background_author
    artwork_id = background.illust_id or record.background_illust_id

    return CheckinCardViewModel(
        date_label=_date_label(record.date_key),
        title=_truncate_display(title, 12),
        badges=tuple(_truncate_display(badge, 8) for badge in badges)[:2],
        event_label=_one_line(event_label),
        username=_one_line(record.username or "访客"),
        avatar_url=_one_line(avatar_url),
        user_title=_truncate_display(user_title, 16),
        bot_name=_truncate_display(bot_name or record.bot_name or "neko", 16),
        greeting=_truncate_display(greeting or record.note or "明天也要来哦", 44),
        greeting_source=_one_line(record.greeting_source or "local"),
        secondary_note=_truncate_display(secondary_note, 44),
        coins_reward=int(record.coins_reward),
        affection_reward=float(record.affection_reward),
        boost_multiplier=float(record.boost_multiplier or 1.0),
        boost_status_text=boost_status,
        streak_days=int(record.streak_days_after),
        total_days=int(record.total_days_after),
        coins_total=int(record.total_coins_after),
        affection_value=snapshot_affection,
        affection_level=str(level["name"]),
        affection_next_text=_affection_next_text(snapshot_affection, level),
        milestone_next_text=_milestone_next_text(int(record.total_days_after)),
        artwork_title=_truncate_display(artwork_title, 18),
        artwork_author=_truncate_display(artwork_author, 12),
        artwork_id=_truncate_display(artwork_id, 24),
        artwork_url=_file_to_data_url(background.image_path),
        artwork_aspect_ratio=_artwork_aspect_ratio(background),
    )


def build_checkin_card_data(
    *,
    profile: CheckinProfile,
    record: CheckinRecord,
    bot_name: str,
    avatar_url: str = "",
    background: CardBackground | None = None,
    user_title: str = "",
    content: CheckinContent | None = None,
    width: int = CHECKIN_CARD_WIDTH,
    height: int = CHECKIN_CARD_HEIGHT,
) -> dict[str, object]:
    if (width, height) != (CHECKIN_CARD_WIDTH, CHECKIN_CARD_HEIGHT):
        raise ValueError(
            f"check-in card canvas must be {CHECKIN_CARD_WIDTH}x{CHECKIN_CARD_HEIGHT}"
        )
    view_model = build_checkin_card_view_model(
        profile=profile,
        record=record,
        bot_name=bot_name,
        avatar_url=avatar_url,
        background=background,
        user_title=user_title,
        content=content,
    )
    data = asdict(view_model)
    for key, value in tuple(data.items()):
        if isinstance(value, str):
            data[key] = escape(value)
    data["badges"] = tuple(escape(badge) for badge in view_model.badges)
    data.update(
        {
            "width": int(width),
            "height": int(height),
            "avatar_initial": escape((view_model.username or "?")[:1]),
            "affection_progress": int(
                affection_level(view_model.affection_value)["progress"]
            ),
            "affection_value_label": f"{view_model.affection_value:.2f}",
            "affection_reward_label": f"{view_model.affection_reward:.2f}",
            "boost_multiplier_label": f"{view_model.boost_multiplier:g}",
            "artwork_credit": escape(_artwork_credit(view_model)),
        }
    )
    return data


def _artwork_credit(view_model: CheckinCardViewModel) -> str:
    if not any(
        (view_model.artwork_title, view_model.artwork_author, view_model.artwork_id)
    ):
        return "今日作品：暂无合适的竖向作品"
    title = view_model.artwork_title or "无标题"
    author = f" / {view_model.artwork_author}" if view_model.artwork_author else ""
    pixiv = f" · Pixiv {view_model.artwork_id}" if view_model.artwork_id else ""
    return f"今日作品：{title}{author}{pixiv}"


def _file_to_data_url(path: str) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    try:
        from io import BytesIO

        from PIL import Image as PILImage

        target_width = CHECKIN_CARD_WIDTH
        target_height = CHECKIN_CARD_HEIGHT
        with PILImage.open(file_path) as img:
            img = img.convert("RGB")
            scale = max(target_width / img.width, target_height / img.height)
            scaled = img.resize(
                (
                    max(target_width, round(img.width * scale)),
                    max(target_height, round(img.height * scale)),
                ),
                PILImage.Resampling.LANCZOS,
            )
            left = (scaled.width - target_width) // 2
            top = int((scaled.height - target_height) * HEADSHOT_CROP_TOP_RATIO)
            cropped = scaled.crop(
                (left, top, left + target_width, top + target_height)
            )
            buf = BytesIO()
            cropped.save(
                buf, format="JPEG", quality=CHECKIN_CARD_JPEG_QUALITY, optimize=True
            )
            data = base64.b64encode(buf.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{data}"
    except Exception as e:
        logger.warning(f"签到背景图片处理失败: {file_path} - {e}")
        return ""
