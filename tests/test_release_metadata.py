from __future__ import annotations

from datetime import date
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _match(pattern: str, path: Path) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"pattern not found in {path}"
    return match.group(1)


def test_release_versions_are_consistent() -> None:
    metadata_version = _match(r"^version:\s*(\S+)$", ROOT / "metadata.yaml")
    runtime_version = _match(r'^PLUGIN_VERSION\s*=\s*"([^"]+)"$', ROOT / "main.py")
    readme_version = _match(r"version-([0-9]+\.[0-9]+\.[0-9]+)-", ROOT / "README.md")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    first_heading = re.search(r"^## (.+)$", changelog, re.MULTILINE)
    assert first_heading is not None
    release = re.fullmatch(
        r"(v\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)", first_heading.group(1)
    )
    assert release is not None, "首个版本段必须是带日期的正式版本，不能跳过待发布标题"
    changelog_version, release_date = release.groups()
    assert date.fromisoformat(release_date) <= date.today()

    assert metadata_version == runtime_version == changelog_version
    assert readme_version == metadata_version.removeprefix("v")
    anchor = f"{changelog_version.replace('.', '')}-{release_date}"
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    link = re.search(r"\[[^\]]*更新日志\]\(([^)]+)\)", readme)
    assert link is not None and link.group(1) == f"CHANGELOG.md#{anchor}"
