from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


DEFAULT_CHECKIN_THEME_ID = "default"


@dataclass(frozen=True)
class CheckinTheme:
    theme_id: str
    name: str
    directory: str
    template_version: str
    description: str
    preview_image: str
    free: bool = False

    def template_dir(self, plugin_root: Path) -> Path:
        return plugin_root / "templates" / self.directory

    def preview_path(self, plugin_root: Path) -> Path:
        return plugin_root / self.preview_image


CHECKIN_THEMES: dict[str, CheckinTheme] = {
    DEFAULT_CHECKIN_THEME_ID: CheckinTheme(
        theme_id=DEFAULT_CHECKIN_THEME_ID,
        name="米白",
        directory="checkin_card_v2",
        template_version="v2",
        description="暖纸便签、画册排版与竖向作品相框",
        preview_image="docs/images/checkin-card-v2-template-preview.png",
        free=True,
    ),
    "01": CheckinTheme(
        theme_id="01",
        name="浅蓝",
        directory="checkin_card_liked/01_stellar_ticket",
        template_version="stellar-v4",
        description="浅色列车票券与星轨观察窗",
        preview_image="templates/checkin_card_liked/01_stellar_ticket/preview.png",
    ),
    "02": CheckinTheme(
        theme_id="02",
        name="红黑",
        directory="checkin_card_liked/02_phantom_coop",
        template_version="phantom-v2",
        description="红黑白朋克界面与撕裂立绘窗",
        preview_image="templates/checkin_card_liked/02_phantom_coop/preview.png",
    ),
    "03": CheckinTheme(
        theme_id="03",
        name="黄黑",
        directory="checkin_card_liked/03_proxy_license",
        template_version="proxy-v3",
        description="工业终端、警示条与战术 HUD",
        preview_image="templates/checkin_card_liked/03_proxy_license/preview.png",
    ),
}


def resolve_checkin_theme(value: object) -> CheckinTheme | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.lower()
    aliases = {
        "默认": "default",
        "免费": "default",
        "default": "default",
        "v2": "default",
        "白": "default",
        "米白": "default",
        "便签": "default",
        "便签画册": "default",
        "蓝": "01",
        "浅蓝": "01",
        "星穹乘车凭证": "01",
        "红": "02",
        "红黑": "02",
        "怪盗契约卡": "02",
        "黄": "03",
        "黄黑": "03",
        "绳网控制终端": "03",
    }
    if normalized in aliases:
        return CHECKIN_THEMES[aliases[normalized]]
    if normalized.isdigit():
        normalized = normalized.zfill(2)
    direct = CHECKIN_THEMES.get(normalized)
    if direct is not None:
        return direct
    for theme in CHECKIN_THEMES.values():
        if raw in {theme.name, theme.directory, Path(theme.directory).name}:
            return theme
    return None


def get_checkin_theme(theme_id: object) -> CheckinTheme:
    return resolve_checkin_theme(theme_id) or CHECKIN_THEMES[DEFAULT_CHECKIN_THEME_ID]
