"""在本地或 PR Quality Gate 中运行插件的静态检查与测试。"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from collections.abc import Iterator, Sequence
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PAGE_DIR = REPOSITORY_ROOT / "pages" / "pluginCenter"
JS_TEST_DIR = REPOSITORY_ROOT / "tests" / "js"

# 有界词法扫描：先区分注释、字符串与代码，再识别字面量引用。
_CODE_TOKEN = re.compile(
    r"""(?P<comment>/\*[\s\S]*?\*/)|(?P<space>\s+)"""
    r"""|(?P<string>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
    r"""|(?P<template>`(?:\\.|[^`\\])*`)"""
    r"""|(?P<operator>\+\+|--)|(?P<word>[-\w$]+)|(?P<punct>.)""",
    re.DOTALL,
)
_REGEX_LITERAL = re.compile(r"/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\r\n\[])+)/[A-Za-z]*")

# 外部地址、协议相对、根绝对、纯锚点与 data: 都不是页面目录内的资源。
_EXTERNAL_REFERENCE = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*:|//|/|#)")


class _HTMLReferences(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.references.extend(
            value for key, value in attrs if key in {"src", "href"} and value
        )


def _code_tokens(source: str, *, javascript: bool) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(source):
        if javascript and source.startswith("//", position):
            newline = source.find("\n", position)
            position = len(source) if newline < 0 else newline + 1
            continue
        previous_kind, previous = tokens[-1] if tokens else ("punct", "")
        match = _CODE_TOKEN.match(source, position)
        assert match is not None
        kind, value = match.lastgroup, match.group()
        if kind in {"space", "comment"}:
            position = match.end()
            continue
        starts_expression = (
            previous_kind == "punct" and previous not in {")", "]", "}"}
        ) or previous in {"return", "throw", "case", "yield"}
        if javascript and source[position] == "/" and starts_expression:
            literal = _REGEX_LITERAL.match(source, position)
            if literal:
                tokens.append(("regex", literal.group()))
                position = literal.end()
                continue
        position = match.end()
        if kind == "string":
            value = value[1:-1]
        tokens.append((kind, value))
    return tokens


def _code_references(source: str, *, javascript: bool) -> Iterator[str]:
    tokens = _code_tokens(source, javascript=javascript)
    for index, (kind, value) in enumerate(tokens):
        following = tokens[index + 1 : index + 4]
        if javascript:
            if kind != "word" or (index and tokens[index - 1] == ("punct", ".")):
                continue
            if (
                value in {"from", "import"}
                and following
                and following[0][0] == "string"
            ):
                yield following[0][1]
            elif (
                value == "import"
                and len(following) == 3
                and following[0] == ("punct", "(")
                and following[1][0] == "string"
                and following[2][1] in {")", ","}
            ):
                yield following[1][1]
        elif (
            kind == "word"
            and value.lower() == "url"
            and following
            and following[0] == ("punct", "(")
        ):
            end = index + 2
            while end < len(tokens) and tokens[end] != ("punct", ")"):
                end += 1
            if end < len(tokens):
                yield "".join(part for _, part in tokens[index + 2 : end])
        elif (
            (kind, value) == ("punct", "@")
            and len(following) >= 2
            and following[0] == ("word", "import")
            and following[1][0] == "string"
        ):
            yield following[1][1]


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

    if path.suffix not in {".js", ".mjs", ".html", ".css"}:
        return set()
    source = path.read_text(encoding="utf-8")
    relative_specifier = path.suffix in {".js", ".mjs"}
    references = (
        _HTMLReferences(source).references
        if path.suffix == ".html"
        else _code_references(source, javascript=relative_specifier)
    )
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
