"""Plugin Pages 打包与桥接端点约束；交互状态由JS测试验证。"""

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


def test_plugin_settings_view_wires_schema_editor_and_delegated_policies() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    app = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    settings = (PAGE_DIR / "settings.mjs").read_text(encoding="utf-8")
    assert 'data-view="settings"' in html
    assert 'id="settingsView"' in html
    assert 'id="settingsGroups"' in html
    assert 'id="settingsSaveBtn"' in html
    assert 'id="settingsReloadBtn"' in html
    assert 'id="settingsStatus"' in html
    assert 'aria-live="polite"' in html[html.index('id="settingsStatus"'):html.index('id="settingsStatus"') + 100]
    assert 'import * as settingsModule from "./settings.mjs"' in app
    assert 'apiGet("settings")' in settings
    assert 'apiPost("settings/update"' in settings
    assert 'button.dataset.navigate = field.view || "policies"' in settings
    assert "重新载入会丢弃尚未保存的修改" in settings
    assert "innerHTML" not in settings
    assert "localStorage" not in settings
