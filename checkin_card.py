from __future__ import annotations

from dataclasses import dataclass
import base64
from html import escape
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .checkin import (
    CheckinProfile,
    CheckinRecord,
    affection_level,
    boost_remaining_days,
    is_boost_active,
)


CHECKIN_CARD_WIDTH = 960
CHECKIN_CARD_HEIGHT = 540
CHECKIN_CARD_JPEG_QUALITY = 85

HEADSHOT_CROP_TOP_RATIO = 0.3  # 居中裁剪时的垂直取景偏移：0=贴顶(保留头部) … 1=贴底

CHECKIN_CARD_TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <style>
    * { box-sizing: border-box; }
    body {
      margin: 0;
      width: {{ width }}px;
      height: {{ height }}px;
      overflow: hidden;
      font-family:
        "Noto Sans SC", "Microsoft YaHei", "PingFang SC", "Source Han Sans SC",
        sans-serif;
      color: #f8fbff;
      background: #1e2635;
    }
    .card {
      position: relative;
      width: {{ width }}px;
      height: {{ height }}px;
      overflow: hidden;
      background:
        linear-gradient(135deg, #4d5e68 0%, #263744 46%, #111927 100%);
      isolation: isolate;
    }
    {% if background_url %}
    .bg {
      position: absolute;
      inset: 0;
      width: 100%;
      height: 100%;
      object-fit: cover;
      filter: saturate(0.9) contrast(1.04);
      z-index: -4;
    }
    {% endif %}
    .card::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(90deg, rgba(7, 11, 20, 0.88) 0%, rgba(11, 17, 29, 0.68) 42%, rgba(13, 19, 30, 0.34) 68%, rgba(9, 14, 24, 0.48) 100%),
        linear-gradient(180deg, rgba(255, 255, 255, 0.14) 0%, rgba(0, 0, 0, 0.24) 100%);
      z-index: -3;
    }
    .card::after {
      content: "";
      position: absolute;
      inset: 18px;
      border: 1px solid rgba(255, 255, 255, 0.28);
      border-radius: 8px;
      box-shadow: inset 0 0 0 1px rgba(20, 26, 38, 0.16);
      z-index: -2;
    }
    .shell {
      position: absolute;
      inset: 30px 38px 32px;
      display: grid;
      grid-template-columns: 190px minmax(0, 1fr) 210px;
      grid-template-rows: auto 1fr auto;
      gap: 18px 26px;
      align-items: stretch;
    }
    .identity {
      grid-column: 1;
      grid-row: 1 / 4;
      min-width: 0;
      display: grid;
      grid-template-rows: auto 1fr auto;
      align-items: center;
    }
    .year {
      font-family: Georgia, "Times New Roman", serif;
      font-size: 18px;
      letter-spacing: 0;
      color: rgba(255, 255, 255, 0.7);
    }
    .avatar-wrap {
      width: 136px;
      height: 136px;
      border-radius: 50%;
      align-self: center;
      justify-self: center;
      display: grid;
      place-items: center;
      padding: 8px;
      background:
        conic-gradient(from 210deg, #ffe08a, #76d7ef, #f6a3b8, #ffe08a);
      box-shadow: 0 18px 40px rgba(0, 0, 0, 0.34);
    }
    .avatar {
      width: 120px;
      height: 120px;
      border-radius: 50%;
      display: grid;
      place-items: center;
      overflow: hidden;
      border: 4px solid rgba(255, 255, 255, 0.82);
      background:
        radial-gradient(circle at 38% 28%, #f8fbff 0 13%, transparent 14%),
        linear-gradient(145deg, #7cc7df, #2f6b86);
      color: #f8fbff;
      font-size: 42px;
      font-weight: 800;
    }
    .avatar img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }
    .uid {
      min-width: 0;
      padding: 10px 12px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.12);
      border: 1px solid rgba(255, 255, 255, 0.18);
      color: rgba(255, 255, 255, 0.84);
      font-size: 16px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .main {
      grid-column: 2;
      grid-row: 1 / 3;
      min-width: 0;
      display: flex;
      flex-direction: column;
      justify-content: flex-start;
      padding-top: 4px;
    }
    .eyebrow {
      margin: 0 0 4px;
      color: rgba(255, 255, 255, 0.68);
      font-size: 16px;
      font-weight: 500;
    }
    .title {
      margin: 0;
      font-size: 42px;
      line-height: 1.08;
      font-weight: 800;
      letter-spacing: 0;
      text-shadow: 0 4px 18px rgba(0, 0, 0, 0.36);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .subtitle {
      margin: 10px 0 0;
      color: rgba(255, 255, 255, 0.8);
      font-size: 18px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 26px;
    }
    .metric {
      min-width: 0;
      padding: 13px 12px 12px;
      border-radius: 7px;
      background: rgba(8, 13, 23, 0.48);
      border: 1px solid rgba(255, 255, 255, 0.16);
    }
    .metric span {
      display: block;
      color: rgba(255, 255, 255, 0.66);
      font-size: 13px;
      margin-bottom: 4px;
    }
    .metric strong {
      display: block;
      font-size: 22px;
      line-height: 1.1;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .affection {
      grid-column: 2 / 4;
      grid-row: 3;
      align-self: end;
      min-width: 0;
      height: 78px;
      padding: 14px 16px;
      border-radius: 7px;
      background: rgba(255, 255, 255, 0.13);
      border: 1px solid rgba(255, 255, 255, 0.2);
      backdrop-filter: blur(8px);
    }
    .affection-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: baseline;
      font-size: 17px;
    }
    .affection-head strong { font-size: 20px; }
    .bar {
      margin-top: 10px;
      height: 10px;
      border-radius: 999px;
      background: rgba(255, 255, 255, 0.2);
      overflow: hidden;
    }
    .bar i {
      display: block;
      height: 100%;
      width: {{ affection_progress }}%;
      border-radius: inherit;
      background: linear-gradient(90deg, #fee38c, #87def0);
    }
    .side {
      grid-column: 3;
      grid-row: 1 / 3;
      display: flex;
      flex-direction: column;
      gap: 12px;
      min-width: 0;
      align-self: start;
    }
    .reward, .memo {
      border-radius: 7px;
      border: 1px solid rgba(255, 255, 255, 0.2);
      background: rgba(12, 19, 31, 0.46);
      backdrop-filter: blur(8px);
      padding: 16px 18px;
      min-width: 0;
    }
    .section-title {
      margin: 0 0 12px;
      font-size: 16px;
      color: rgba(255, 255, 255, 0.7);
    }
    .reward-line {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: baseline;
      font-size: 17px;
      margin-top: 10px;
      white-space: nowrap;
    }
    .reward-line strong { font-size: 22px; }
    .memo {
      min-height: 104px;
      display: flex;
      flex-direction: column;
      justify-content: center;
    }
    .memo strong {
      display: block;
      font-size: 17px;
      line-height: 1.3;
      margin-bottom: 5px;
    }
    .memo span {
      display: block;
      font-size: 15px;
      color: rgba(255, 255, 255, 0.74);
      line-height: 1.45;
    }
  </style>
</head>
<body>
  <div class="card">
    {% if background_url %}<img class="bg" src="{{ background_url }}" alt="" />{% endif %}
    <div class="shell">
      <section class="identity">
        <div class="year">{{ bot_name }}@{{ year }}</div>
        <div class="avatar-wrap">
          <div class="avatar">
            {% if avatar_url %}
            <img src="{{ avatar_url }}" alt="" />
            {% else %}
            {{ avatar_initial }}
            {% endif %}
          </div>
        </div>
        <div class="uid">UID: {{ user_id }}</div>
      </section>
      <main class="main">
        <p class="eyebrow">{{ date_label }}</p>
        <h1 class="title">今日签到</h1>
        <p class="subtitle">{{ bot_name }} 对你的态度：{{ affection_level }}</p>
        <div class="metrics">
          <div class="metric"><span>累计签到</span><strong>{{ total_days }} 天</strong></div>
          <div class="metric"><span>连续签到</span><strong>{{ streak_days }} 天</strong></div>
          <div class="metric"><span>金币余额</span><strong>{{ coins }}</strong></div>
        </div>
      </main>
      <aside class="side">
        <section class="reward">
          <p class="section-title">今日奖励</p>
          <div class="reward-line"><span>金币</span><strong>+{{ coins_reward }}</strong></div>
          <div class="reward-line"><span>好感度</span><strong>+{{ affection_reward }}</strong></div>
        </section>
        <section class="memo">
          <strong>{{ memo_title }}</strong>
          <span>{{ memo_text }}</span>
        </section>
      </aside>
      <section class="affection">
        <div class="affection-head">
          <span>好感度 {{ affection }}</span>
          <strong>{{ affection_level }}</strong>
        </div>
        <div class="bar"><i></i></div>
      </section>
    </div>
  </div>
</body>
</html>
"""


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


def build_checkin_card_data(
    *,
    profile: CheckinProfile,
    record: CheckinRecord,
    bot_name: str,
    avatar_url: str = "",
    background: CardBackground | None = None,
    width: int = CHECKIN_CARD_WIDTH,
    height: int = CHECKIN_CARD_HEIGHT,
) -> dict[str, object]:
    background = background or CardBackground()
    level = affection_level(profile.affection)
    active_boost = is_boost_active(profile, record.date_key)
    remaining = boost_remaining_days(profile, record.date_key)
    if active_boost:
        memo_title = "好感度双倍加持中"
        memo_text = f"剩余 {remaining} 天"
    else:
        memo_title = "今日小记"
        memo_text = record.note or "明天也要来哦"

    return {
        "width": int(width),
        "height": int(height),
        "year": escape(record.date_key[:4]),
        "date_label": escape(record.date_key),
        "bot_name": escape(bot_name or record.bot_name or "neko"),
        "user_id": escape(record.user_id),
        "username": escape(record.username or record.user_id),
        "avatar_url": escape(avatar_url),
        "avatar_initial": escape((record.username or record.user_id or "?")[:1]),
        "background_url": _file_to_data_url(background.image_path),
        "coins": profile.coins,
        "affection": f"{profile.affection:.2f}",
        "affection_level": escape(str(level["name"])),
        "affection_progress": int(level["progress"]),
        "total_days": profile.total_days,
        "streak_days": profile.streak_days,
        "coins_reward": record.coins_reward,
        "affection_reward": f"{record.affection_reward:.2f}",
        "memo_title": escape(memo_title),
        "memo_text": escape(memo_text),
    }


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
        target_height = CHECKIN_CARD_HEIGHT  # 卡片本身即 16:9
        with PILImage.open(file_path) as img:
            img = img.convert("RGB")
            # 等比缩放到「恰好覆盖」目标框，避免水平/垂直拉伸
            scale = max(target_width / img.width, target_height / img.height)
            scaled = img.resize(
                (
                    max(target_width, round(img.width * scale)),
                    max(target_height, round(img.height * scale)),
                ),
                PILImage.Resampling.LANCZOS,
            )
            # 居中裁剪到目标尺寸；垂直方向偏上取景以保留头部
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
