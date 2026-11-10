"""在本地或 PR Quality Gate 中运行插件的静态检查与测试。"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PAGE_DIR = REPOSITORY_ROOT / "pages" / "pluginCenter"
JS_TEST_DIR = REPOSITORY_ROOT / "tests" / "js"

# 相对引用：模块的静态/动态/副作用 import、HTML 的 src/href、CSS 的 url()/@import。
# JS 只认 ./ 与 ../ 说明符，避免把裸模块名当本地文件；HTML／CSS 允许裸相对名。
# 同时匹配 ../，交由 resource_closure 拒绝逃出页面目录。
_JS_REFERENCE_PATTERNS = (
    r"""from\s*['"]([^'"]+)['"]""",
    r"""import\s*\(\s*['"]([^'"]+)['"]""",
    r"""import\s+['"]([^'"]+)['"]""",
)
_HTML_REFERENCE_PATTERN = r"""(?:src|href)\s*=\s*['"]([^'"]+)['"]"""
_CSS_URL_PATTERN = r"""url\(\s*['"]?([^'")]+)['"]?\s*\)"""
_CSS_IMPORT_PATTERN = r"""@import\s+(?:url\(\s*)?['"]([^'"]+)['"]"""

# 外部地址、协议相对、根绝对、纯锚点与 data: 都不是页面目录内的资源。
_EXTERNAL_REFERENCE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//|/|#)")


def _run_check(command: Sequence[str]) -> None:
    """在仓库根目录执行一项检查，并保留真实退出码。"""

    print(f"$ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)


def _relative(path: Path) -> str:
    return path.relative_to(REPOSITORY_ROOT).as_posix()


def _frontend_modules() -> list[str]:
    """页面目录下的全部脚本模块，新增文件自动纳入语法检查。"""

    return [
        _relative(path)
        for path in sorted(PAGE_DIR.glob("*.js")) + sorted(PAGE_DIR.glob("*.mjs"))
    ]


def _js_tests() -> list[str]:
    """tests/js 下的全部长期状态测试，新增文件自动纳入执行。"""

    return [_relative(path) for path in sorted(JS_TEST_DIR.glob("*.test.mjs"))]


def _local_reference(reference: str, *, relative_specifier: bool) -> str | None:
    """把引用还原成页面目录内的相对路径；外部地址、根绝对与纯锚点返回 None。"""

    reference = reference.strip()
    if not reference or _EXTERNAL_REFERENCE.match(reference):
        return None
    path = urlsplit(reference).path
    if not path:
        return None
    if relative_specifier and not path.startswith(("./", "../")):
        return None
    return path


def _local_resources(path: Path) -> set[Path]:
    """页面、样式或模块直接引用的同目录相对资源；忽略 query/hash 与绝对地址。"""

    if path.suffix in {".js", ".mjs"}:
        patterns, relative_specifier = _JS_REFERENCE_PATTERNS, True
    elif path.suffix == ".html":
        patterns, relative_specifier = (_HTML_REFERENCE_PATTERN,), False
    elif path.suffix == ".css":
        patterns, relative_specifier = (_CSS_URL_PATTERN, _CSS_IMPORT_PATTERN), False
    else:
        return set()
    source = path.read_text(encoding="utf-8")
    references = {ref for pattern in patterns for ref in re.findall(pattern, source)}
    resolved = set()
    for reference in references:
        local = _local_reference(reference, relative_specifier=relative_specifier)
        if local:
            resolved.add((path.parent / local).resolve())
    return resolved


def resource_closure(entry: Path, root: Path | None = None) -> set[Path]:
    """从入口递归解析相对资源；缺失或逃出页面目录时直接失败。"""

    boundary = (root or entry.parent).resolve()
    seen: set[Path] = set()
    pending = [entry.resolve()]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        if not path.is_relative_to(boundary):
            raise SystemExit(f"页面静态资源越出页面目录: {path}")
        if not path.is_file():
            raise SystemExit(f"页面静态资源缺失: {path}")
        seen.add(path)
        pending.extend(_local_resources(path) - seen)
    return seen


def _check_page_resources() -> None:
    """从页面入口递归解析 HTML／CSS／模块引用，缺失时门禁失败。"""

    seen = resource_closure(PAGE_DIR / "index.html", PAGE_DIR)
    print(f"页面静态资源递归检查通过: files={len(seen)}", flush=True)


def main() -> int:
    """按 Quality Gate 的固定顺序执行所有检查。"""

    python = sys.executable
    modules = _frontend_modules()
    js_tests = _js_tests()
    if not modules or not js_tests:
        raise SystemExit("质量门禁未发现前端模块或 JS 测试文件")
    _check_page_resources()
    checks: tuple[tuple[str, ...], ...] = (
        (
            python,
            "-m",
            "compileall",
            "-q",
            "main.py",
            "group_safety.py",
            "checkin",
            "pixiv",
            "tools",
            "plugin_api",
            "scripts/ci",
            "tests",
        ),
        (python, "-m", "json.tool", "_conf_schema.json"),
        *(("node", "--check", module) for module in modules),
        *(("node", "--test", test) for test in js_tests),
        (python, "-m", "pytest", "-v", "tests"),
    )
    for command in checks:
        _run_check(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
