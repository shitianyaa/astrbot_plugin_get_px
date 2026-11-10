"""Plugin Pages 打包资源与桥接端点契约；交互行为由浏览器回归验证。"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PAGE_DIR = REPOSITORY_ROOT / "pages" / "pluginCenter"
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts" / "ci"))

import check_plugin_quality_gate as gate  # noqa: E402


def test_page_resource_closure_covers_entry_styles_and_modules():
    closure = gate.resource_closure(PAGE_DIR / "index.html", PAGE_DIR)
    names = {path.name for path in closure}
    assert "index.html" in names
    assert {"app.js", "settings.mjs", "safety.mjs", "ui.mjs"} <= names
    assert {path.name for path in PAGE_DIR.glob("*.mjs")} <= names
    assert {"styles.css", "safety.css"} <= names


def test_page_modules_only_call_relative_bridge_endpoints():
    closure = gate.resource_closure(PAGE_DIR / "index.html", PAGE_DIR)
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in closure
        if path.suffix in {".js", ".mjs"}
    )
    endpoints = re.findall(r'(?:apiGet|apiPost|download|upload)\("([^"]+)"', source)
    assert endpoints
    assert all(not endpoint.startswith("/") for endpoint in endpoints)


def test_resource_closure_reports_missing_nested_assets(tmp_path: Path):
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text(
        '<link rel="stylesheet" href="./theme.css?v=2" />'
        '<script type="module" src="./main.mjs"></script>',
        encoding="utf-8",
    )
    (page / "theme.css").write_text(
        '@import "./base.css";\n.hero { background: url("./img/hero.png#x"); }\n',
        encoding="utf-8",
    )
    (page / "base.css").write_text(".a { color: #000; }\n", encoding="utf-8")
    (page / "main.mjs").write_text(
        "import { helper } from './helper.mjs';\n"
        "import './side.mjs';\n"
        "const lazy = () => import('./lazy.mjs');\n",
        encoding="utf-8",
    )
    (page / "helper.mjs").write_text("export const helper = 1;\n", encoding="utf-8")
    (page / "side.mjs").write_text("export default 1;\n", encoding="utf-8")
    (page / "lazy.mjs").write_text("export default 2;\n", encoding="utf-8")

    with pytest.raises(SystemExit):
        gate.resource_closure(page / "index.html", page)

    (page / "img").mkdir()
    (page / "img" / "hero.png").write_bytes(b"png")
    names = {path.name for path in gate.resource_closure(page / "index.html", page)}
    assert {
        "theme.css",
        "base.css",
        "main.mjs",
        "helper.mjs",
        "side.mjs",
        "lazy.mjs",
    } <= names
    assert "hero.png" in names


def test_resource_closure_accepts_bare_relative_names_and_ignores_external(
    tmp_path: Path,
):
    page = tmp_path / "page"
    (page / "fonts").mkdir(parents=True)
    (page / "index.html").write_text(
        "<link rel='stylesheet' href='theme.css'>"
        "<a href='#main'>跳转</a>"
        "<a href='https://example.com/other.css'>外部</a>"
        "<script type='module' src='//cdn.example.com/other.mjs'></script>"
        "<img src='/root.png'>"
        '<img src="data:image/png;base64,AAAA">',
        encoding="utf-8",
    )
    (page / "theme.css").write_text(
        "@import 'base.css';\n"
        "@font-face { src: url(fonts/a.woff2); }\n"
        ".hero { background: url('/root.png'); }\n",
        encoding="utf-8",
    )
    (page / "base.css").write_text(".a { color: #000; }\n", encoding="utf-8")
    (page / "fonts" / "a.woff2").write_bytes(b"woff2")

    names = {path.name for path in gate.resource_closure(page / "index.html", page)}
    assert names == {"index.html", "theme.css", "base.css", "a.woff2"}


def test_module_bare_specifiers_are_not_local_files(tmp_path: Path):
    page = tmp_path / "page"
    page.mkdir()
    (page / "index.html").write_text(
        '<script type="module" src="./main.mjs"></script>', encoding="utf-8"
    )
    (page / "main.mjs").write_text(
        "import quart from'quart';\nimport { a } from'./a.mjs';\n",
        encoding="utf-8",
    )
    (page / "a.mjs").write_text("export const a = 1;\n", encoding="utf-8")

    names = {path.name for path in gate.resource_closure(page / "index.html", page)}
    assert names == {"index.html", "main.mjs", "a.mjs"}


def test_resource_closure_rejects_paths_outside_page_directory(tmp_path: Path):
    page = tmp_path / "page"
    page.mkdir()
    (tmp_path / "outside.css").write_text(".a { color: #000; }\n", encoding="utf-8")
    (page / "index.html").write_text(
        '<link rel="stylesheet" href="../outside.css" />', encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        gate.resource_closure(page / "index.html", page)
