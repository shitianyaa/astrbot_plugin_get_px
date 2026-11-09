"""在本地或 PR Quality Gate 中运行插件的静态检查与测试。"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PAGE_DIR = REPOSITORY_ROOT / "pages" / "pluginCenter"
JS_TEST_DIR = REPOSITORY_ROOT / "tests" / "js"


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


def _local_resources(path: Path) -> set[Path]:
    """页面或模块直接引用的同目录资源；只认相对路径。"""

    if path.suffix in {".js", ".mjs"}:
        pattern = r'from "(\./[^"]+)"'
    elif path.suffix == ".html":
        pattern = r'(?:src|href)="(\./[^"]+)"'
    else:
        return set()
    source = path.read_text(encoding="utf-8")
    return {(path.parent / match).resolve() for match in re.findall(pattern, source)}


def _check_page_resources() -> None:
    """从页面入口递归解析 HTML／CSS／模块引用，缺失时门禁失败。"""

    entry = (PAGE_DIR / "index.html").resolve()
    seen: set[Path] = set()
    pending = [entry]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        if not path.is_file():
            raise SystemExit(f"页面静态资源缺失: {path}")
        seen.add(path)
        pending.extend(_local_resources(path) - seen)
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
