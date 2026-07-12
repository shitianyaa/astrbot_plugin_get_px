from __future__ import annotations

import re
from pathlib import Path


PAGE_DIR = Path(__file__).resolve().parents[1] / "pages" / "imageHistory"


def test_image_history_page_uses_split_assets() -> None:
    html = (PAGE_DIR / "index.html").read_text(encoding="utf-8")
    expected_css = (
        "./css/tokens.css",
        "./css/layout.css",
        "./css/gallery.css",
        "./css/overlays.css",
        "./css/responsive.css",
    )
    positions = [html.index(f'href="{path}"') for path in expected_css]
    assert positions == sorted(positions)
    assert 'src="./js/app.js"' in html
    assert not (PAGE_DIR / "app.js").exists()
    assert not (PAGE_DIR / "style.css").exists()


def test_image_history_javascript_modules_and_bridge_paths() -> None:
    js_dir = PAGE_DIR / "js"
    module_names = ("core.js", "render.js", "actions.js", "data.js", "app.js")
    source = "\n".join(
        (js_dir / name).read_text(encoding="utf-8") for name in module_names
    )
    assert 'window.AstrBotPluginPage' in source
    assert 'bridge.ready()' in source
    endpoints = re.findall(
        r'bridge\.(?:apiGet|apiPost|upload)\("([^"]+)"', source
    )
    assert endpoints
    assert all(not endpoint.startswith("/") for endpoint in endpoints)
    assert all("?" not in endpoint and "#" not in endpoint for endpoint in endpoints)
    app_source = (js_dir / "app.js").read_text(encoding="utf-8")
    assert "findRecord, renderContent, renderCheckinImportPanel" in app_source


def test_image_history_stylesheets_keep_responsive_and_dark_mode_rules() -> None:
    css_dir = PAGE_DIR / "css"
    stylesheet_names = (
        "tokens.css",
        "layout.css",
        "gallery.css",
        "overlays.css",
        "responsive.css",
    )
    source = "\n".join(
        (css_dir / name).read_text(encoding="utf-8") for name in stylesheet_names
    )
    assert ":root" in source
    assert "@media (prefers-color-scheme: dark)" in source
    assert "@media (max-width: 760px)" in source
    assert "@media (prefers-reduced-motion: reduce)" in source
    for name in stylesheet_names:
        stylesheet = (css_dir / name).read_text(encoding="utf-8")
        assert stylesheet.count("/*") == stylesheet.count("*/"), name
