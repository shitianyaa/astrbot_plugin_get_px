from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _match(pattern: str, path: Path) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"pattern not found in {path}"
    return match.group(1)


def test_release_versions_are_consistent() -> None:
    metadata_version = _match(r"^version:\s*(\S+)$", ROOT / "metadata.yaml")
    runtime_version = _match(
        r'^PLUGIN_VERSION\s*=\s*"([^"]+)"$', ROOT / "main.py"
    )
    readme_version = _match(
        r"version-([0-9]+\.[0-9]+\.[0-9]+)-", ROOT / "README.md"
    )
    changelog_version = _match(
        r"^##\s+(v\d+\.\d+\.\d+)\s+\(", ROOT / "CHANGELOG.md"
    )

    assert metadata_version == runtime_version == changelog_version
    assert readme_version == metadata_version.removeprefix("v")
