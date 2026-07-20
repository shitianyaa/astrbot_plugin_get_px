from __future__ import annotations

import re
from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "pluginCenter"


def test_plugin_center_page_exposes_management_workspaces() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert "插件管理中心" in html
    assert 'data-view="ranking"' in html
    assert 'data-view="members"' in html
    assert 'data-view="safety"' in html
    assert 'data-view="data"' in html
    assert "群签到轨道" in html
    assert "签到成员数值" in html
    assert "内置安全词" in html
    assert "签到数据管理" in html
    assert "imageHistory" not in html
    assert "cacheStats" not in html
    assert "schema v7" in html
    assert "schema v6" in html


def test_plugin_center_import_accepts_json_backups_only() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    script = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert 'accept="application/json,.json"' in html
    assert "只能选择 JSON 备份文件。" in script
    assert "备份文件不能超过 5 MiB。" in script
    assert "恢复签到数据" in script
    assert "sqlite" not in html.lower()
    assert "sqlite" not in script.lower()


def test_plugin_center_uses_relative_bridge_endpoints() -> None:
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert "window.AstrBotPluginPage" in source
    assert "bridge.ready()" in source
    assert 'bridge.download("checkin-export"' in source
    assert 'bridge.upload("checkin-import"' in source
    endpoints = re.findall(r'(?:apiGet|apiPost)\("([^"]+)"', source)
    assert endpoints
    assert all(not endpoint.startswith("/") for endpoint in endpoints)
    assert "image-history" not in source
    assert "cache_cleanup" not in source


def test_plugin_center_keeps_responsive_and_accessible_states() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    source = (PAGE_DIR / "styles.css").read_text(encoding="utf-8")
    script = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert 'name="theme-color"' in html
    assert 'class="skip-link"' in html
    assert 'id="globalError"' in html
    assert 'id="retryAllBtn"' in html
    assert 'name="custom_safety_term"' in html
    assert 'name="pixiv_illust_id"' in html
    assert 'name="checkin_backup"' in html
    assert 'name="checkin_member_search"' in html
    assert 'id="memberDialog"' in html
    assert ":root" in source
    assert "@media (max-width: 900px)" in source
    assert "@media (max-width: 620px)" in source
    assert "@media (prefers-reduced-motion: reduce)" in source
    assert ":focus-visible" in source
    assert "::file-selector-button" in source
    assert "transition: all" not in source
    assert "onerror=" not in script
    assert "Promise.allSettled" in script
    assert "MAX_BACKUP_BYTES" in script
    assert 'apiGet("checkin-members"' in script
    assert 'apiPost("checkin-members/update"' in script
    assert source.count("/*") == source.count("*/")
