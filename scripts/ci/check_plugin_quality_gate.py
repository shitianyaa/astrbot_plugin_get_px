"""在本地或 PR Quality Gate 中运行插件的静态检查与测试。"""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _run_check(command: Sequence[str]) -> None:
    """在仓库根目录执行一项检查，并保留真实退出码。"""

    print(f"$ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=REPOSITORY_ROOT, check=True)


def main() -> int:
    """按 Quality Gate 的固定顺序执行所有检查。"""

    python = sys.executable
    checks: tuple[tuple[str, ...], ...] = (
        (
            python,
            "-m",
            "compileall",
            "-q",
            "main.py",
            "checkin",
            "pixiv",
            "plugin_api",
            "scripts/ci",
            "tests",
        ),
        (python, "-m", "json.tool", "_conf_schema.json"),
        ("node", "--check", "pages/pluginCenter/app.js"),
        (python, "-m", "pytest", "-v"),
    )
    for command in checks:
        _run_check(command)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
