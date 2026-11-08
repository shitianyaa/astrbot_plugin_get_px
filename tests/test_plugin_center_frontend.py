"""Plugin Pages 打包与桥接端点约束；交互状态由JS测试验证。"""

import re
from pathlib import Path

PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "pluginCenter"


def test_plugin_center_import_accepts_json_backups_only():
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    assert 'accept="application/json,.json"' in html


def test_plugin_center_uses_relative_bridge_endpoints():
    source = (PAGE_DIR / "app.js").read_text(encoding="utf-8")
    endpoints = re.findall(r'(?:apiGet|apiPost|download|upload)\("([^"]+)"', source)
    assert endpoints
    assert all(not endpoint.startswith("/") for endpoint in endpoints)
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    resources = re.findall(r'(?:src|href)="(\./[^"]+)"', html)
    assert resources
    assert all((PAGE_DIR / resource).is_file() for resource in resources)
