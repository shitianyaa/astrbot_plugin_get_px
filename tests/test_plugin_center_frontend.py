"""Plugin Pages 打包、桥接端点与页面接线约束；交互状态由 JS 测试验证。"""

import re
from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "pluginCenter"


def test_plugin_center_import_accepts_json_backups_only():
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert 'accept="application/json,.json"' in html


def test_plugin_center_uses_relative_bridge_endpoints():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    module_paths = re.findall(r'from "(\./[^"]+)"', source)
    assert all((PAGE_DIR / path).is_file() for path in module_paths)
    source += "\n".join((PAGE_DIR / path).read_text(encoding="utf-8") for path in module_paths)
    endpoints = re.findall(r'(?:apiGet|apiPost|download|upload)\("([^"]+)"', source)
    assert endpoints
    assert all(not endpoint.startswith("/") for endpoint in endpoints)
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    resources = re.findall(r'(?:src|href)="(\./[^"]+)"', html)
    assert resources
    assert all((PAGE_DIR / resource).is_file() for resource in resources)


def test_plugin_center_has_six_views_in_approved_order() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    tabs = re.findall(r'data-view="([a-z]+)"', html)
    assert tabs == ["ranking", "members", "safety", "policies", "data", "settings"]
    for view in tabs:
        assert f'id="{view}View"' in html


def test_plugin_center_theme_follows_host_without_browser_storage() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    app = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    assert 'data-theme' in html
    assert "prefers-color-scheme" in html
    assert "localStorage" not in html
    assert "sessionStorage" not in html
    assert "onContext" in app
    assert "isDark" in app
    assert "localStorage" not in app


def test_plugin_settings_view_wires_schema_editor_and_delegated_policies() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    app = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    settings = (PAGE_DIR / "settings.mjs").read_text(encoding="utf-8")
    ui = (PAGE_DIR / "ui.mjs").read_text(encoding="utf-8")
    assert 'data-view="settings"' in html
    assert 'id="settingsView"' in html
    assert 'id="settingsSearch"' in html
    assert 'id="settingsNav"' in html
    assert 'id="settingsGroups"' in html
    assert 'id="settingsSaveBtn"' in html
    assert 'id="settingsReloadBtn"' in html
    assert 'id="settingsStatus"' in html
    assert 'aria-live="polite"' in html[html.index('id="settingsStatus"'):html.index('id="settingsStatus"') + 100]
    assert 'import * as settingsModule from "./settings.mjs"' in app
    assert 'import { createSelect } from "./ui.mjs"' in app
    assert 'import { createSelect, createSwitch } from "./ui.mjs"' in settings
    assert 'apiGet("settings")' in settings
    assert 'apiPost("settings/update"' in settings
    assert 'button.dataset.navigate = field.view || "policies"' in settings
    assert "重新载入会丢弃尚未保存的修改" in settings
    assert "window.confirm" not in settings
    for module in (settings, ui):
        assert "innerHTML" not in module
        assert "localStorage" not in module
    assert "createSwitch" in ui
    assert "switch-track" in ui
