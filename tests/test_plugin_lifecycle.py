from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.ci import check_plugin_lifecycle as lifecycle
from scripts.ci.check_plugin_lifecycle import LifecycleRuntime, RuntimeSnapshot


@pytest.mark.parametrize(
    ("plugin_relative", "root_relative"),
    [
        ("plugin", "plugin/astrbot-root"),
        ("astrbot-root/plugin", "astrbot-root"),
    ],
)
def test_stage_plugin_rejects_containing_paths(
    tmp_path: Path,
    plugin_relative: str,
    root_relative: str,
) -> None:
    plugin_dir = tmp_path / plugin_relative
    astrbot_root = tmp_path / root_relative
    plugin_dir.mkdir(parents=True)
    astrbot_root.mkdir(parents=True, exist_ok=True)

    with pytest.raises(ValueError, match="不能互相包含"):
        lifecycle.stage_plugin(
            plugin_dir=plugin_dir,
            astrbot_root=astrbot_root,
            plugin_name="example_plugin",
        )


@pytest.mark.asyncio
async def test_lifecycle_uses_registered_name_for_reload_and_uninstall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    astrbot_source = tmp_path / "astrbot-source"
    plugin_dir = tmp_path / "plugin-dir"
    astrbot_source.mkdir()
    plugin_dir.mkdir()

    registered_stars: list[object] = []
    calls: list[tuple[str, str]] = []

    class FakePluginManager:
        async def load(self, *, specified_dir_name: str) -> tuple[bool, None]:
            calls.append(("load", specified_dir_name))
            registered_stars.append(SimpleNamespace(name="registered_name"))
            return True, None

        async def reload(self, *, specified_plugin_name: str) -> tuple[bool, None]:
            calls.append(("reload", specified_plugin_name))
            registered_stars.clear()
            registered_stars.append(SimpleNamespace(name="registered_name"))
            return True, None

        async def uninstall_plugin(self, plugin_name: str) -> None:
            calls.append(("uninstall", plugin_name))
            registered_stars.clear()

    runtime = LifecycleRuntime(
        plugin_manager=FakePluginManager(),
        snapshot_state=lambda: RuntimeSnapshot(),
        snapshot_stars=lambda: tuple(registered_stars),
    )
    monkeypatch.setattr(lifecycle, "stage_plugin", lambda **_: plugin_dir)
    monkeypatch.setattr(lifecycle, "_prepend_sys_path", lambda *_: [])
    monkeypatch.setattr(
        lifecycle,
        "_build_official_runtime",
        lambda *_: runtime,
    )

    await lifecycle.run_lifecycle_check(
        astrbot_source=astrbot_source,
        astrbot_version="v4.27.5",
        plugin_dir=plugin_dir,
        astrbot_root=tmp_path / "astrbot-root",
        plugin_name="plugin_dir",
    )

    assert calls == [
        ("load", "plugin_dir"),
        ("reload", "registered_name"),
        ("uninstall", "registered_name"),
    ]
