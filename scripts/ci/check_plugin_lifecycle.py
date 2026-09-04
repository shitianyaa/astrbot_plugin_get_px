"""使用官方公开 PluginManager API 验证插件生命周期和资源清理。

脚本只用于 PR CI 或本地兼容性检查：把插件源码放入临时 AstrBot 根目录，
调用官方 ``PluginManager.load()``、``reload()`` 和 ``uninstall_plugin()``，
再检查本轮新增的运行时资源是否全部消失。生命周期 harness 不自行拼装
AstrBot 的 terminate/unbind 流程，也不规定插件必须注册任何特定资源。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
import re
import shutil
import sys
import traceback
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_STABLE_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CATCHABLE_ERRORS = (Exception, asyncio.CancelledError)

# 只排除版本控制元数据、宿主虚拟环境和 Python 生成物；插件自己的目录
# （包括 scripts、data、docs、tests）必须原样进入 staging，避免漏测运行时代码。
_STAGING_ARTIFACT_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
_STAGING_ARTIFACT_NAMES_LOWER = {name.lower() for name in _STAGING_ARTIFACT_NAMES}
_STAGING_ARTIFACT_SUFFIXES = {".pyc", ".pyo"}


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """记录检查前后可观察的 AstrBot 运行时资源。"""

    handlers: tuple[object, ...] = ()
    web_apis: tuple[object, ...] = ()
    tasks: tuple[asyncio.Task[Any], ...] = ()


@dataclass(slots=True)
class LifecycleRuntime:
    """官方 PluginManager 和运行时快照函数。"""

    plugin_manager: Any
    snapshot_state: Callable[[], RuntimeSnapshot]
    snapshot_stars: Callable[[], tuple[object, ...]]


@dataclass(frozen=True, slots=True)
class LifecycleReport:
    """一次成功生命周期检查的摘要。"""

    astrbot_version: str
    plugin_name: str
    loaded_resource_counts: tuple[int, int, int]
    reloaded_resource_counts: tuple[int, int, int]


class LifecycleCheckError(RuntimeError):
    """带有明确检查阶段的生命周期错误。"""

    def __init__(self, phase: str, message: str) -> None:
        self.phase = phase
        super().__init__(f"[{phase}] {message}")


def select_latest_stable_version(tags: Iterable[str]) -> str:
    """从标签中选择最高的正式三段式版本，排除 beta/rc 等预发布版本。"""

    candidates: list[tuple[tuple[int, int, int], str]] = []
    for raw_tag in tags:
        if not isinstance(raw_tag, str):
            continue
        tag = raw_tag.strip()
        match = _STABLE_VERSION_RE.fullmatch(tag)
        if match is None:
            continue
        candidates.append((tuple(int(part) for part in match.groups()), tag))
    if not candidates:
        raise ValueError("没有找到符合正式 stable 版本格式的 AstrBot release tag")
    return max(candidates, key=lambda item: item[0])[1]


def _is_staging_artifact(entry: Path) -> bool:
    name = entry.name.lower()
    return (
        name in _STAGING_ARTIFACT_NAMES_LOWER
        or name.startswith(".env")
        or entry.suffix.lower() in _STAGING_ARTIFACT_SUFFIXES
    )


def _validate_plugin_name(plugin_name: str) -> None:
    if _PLUGIN_NAME_RE.fullmatch(plugin_name) is None:
        raise ValueError(
            f"插件名必须是可导入的单一 Python 目录名，实际为 {plugin_name!r}"
        )


def _copy_plugin_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    for entry in sorted(source.iterdir(), key=lambda item: item.name):
        if _is_staging_artifact(entry):
            continue
        if entry.is_symlink():
            # 不跟随可能指向仓库外或宿主机秘密的链接，避免 staging 越界。
            raise ValueError(f"插件源码包含不允许 staging 的符号链接: {entry}")
        target = destination / entry.name
        if entry.is_dir():
            _copy_plugin_tree(entry, target)
        elif entry.is_file():
            shutil.copy2(entry, target)
        else:
            raise OSError(f"无法 staging 非普通文件: {entry}")


def stage_plugin(
    *,
    plugin_dir: str | Path,
    astrbot_root: str | Path,
    plugin_name: str,
) -> Path:
    """把插件源码复制到临时 AstrBot 根目录。"""

    source = Path(plugin_dir).expanduser().resolve()
    root = Path(astrbot_root).expanduser().resolve()
    _validate_plugin_name(plugin_name)
    if not source.is_dir():
        raise ValueError(f"插件目录不存在或不是目录: {source}")
    if root == source or root in source.parents or source in root.parents:
        raise ValueError("ASTRBOT_ROOT 与插件源码目录不能互相包含")
    if root.is_symlink():
        raise ValueError(f"ASTRBOT_ROOT 不能是符号链接: {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"ASTRBOT_ROOT 不是目录: {root}")
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"ASTRBOT_ROOT 必须为空的临时目录: {root}")

    destination = root / "data" / "plugins" / plugin_name
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_plugin_tree(source, destination)
    return destination


def _write_ci_plugin_config(astrbot_root: Path, plugin_name: str) -> None:
    config_path = astrbot_root / "data" / "config" / f"{plugin_name}_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}\n", encoding="utf-8")


def _prepend_sys_path(*paths: Path) -> list[str]:
    inserted: list[str] = []
    for path in paths:
        value = str(path)
        if value not in sys.path:
            sys.path.insert(0, value)
            inserted.append(value)
    return inserted


def _remove_sys_path(values: Sequence[str]) -> None:
    for value in values:
        try:
            sys.path.remove(value)
        except ValueError:
            # 官方模块可能重排 sys.path；目标值不在列表时无需重复处理。
            continue


def _clear_directory_contents(directory: Path) -> None:
    """清理检查写入的目录内容，但保留调用方预先创建的空目录。"""

    for entry in directory.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            entry.unlink()
        else:
            shutil.rmtree(entry)


def _build_official_context(context_cls: type[Any], config: Any) -> Any:
    """按官方 Context 的真实签名注入最小依赖，不自定义替代 Context。"""

    parameters = inspect.signature(context_cls).parameters
    kwargs: dict[str, Any] = {}
    for name, parameter in parameters.items():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if name in {"self", "cls"}:
            continue
        if name in {"config", "astrbot_config"}:
            kwargs[name] = config
        elif parameter.default is inspect.Parameter.empty:
            # 生命周期检查不启动真实平台；Context 只保存这些宿主依赖。
            kwargs[name] = None
    if "config" not in kwargs and "astrbot_config" not in kwargs:
        raise TypeError("官方 Context 构造函数没有可识别的配置参数")
    return context_cls(**kwargs)


def _build_official_runtime(
    astrbot_root: Path,
    plugin_name: str,
) -> LifecycleRuntime:
    """建立官方 Context/PluginManager，并提供资源快照观测边界。"""

    _write_ci_plugin_config(astrbot_root, plugin_name)
    from astrbot.api.star import Context
    from astrbot.core import AstrBotConfig
    from astrbot.core.star.star_handler import star_handlers_registry
    from astrbot.core.star.star_manager import PluginManager

    config = AstrBotConfig(
        config_path=str(astrbot_root / "data" / "cmd_config.json"),
    )
    get_all_stars = getattr(Context, "get_all_stars", None)
    if not callable(get_all_stars):
        raise LifecycleCheckError(
            "runtime",
            "当前 AstrBot 没有可观测的 get_all_stars() 公共接口",
        )
    registered_web_apis = getattr(Context, "registered_web_apis", None)
    if not isinstance(registered_web_apis, list):
        raise LifecycleCheckError(
            "runtime",
            "当前 AstrBot 没有可观测的 registered_web_apis list",
        )
    # Context 当前把 Web API 记录在类级 registry；保留既有内容，交给 baseline
    # 对比只识别本次检查新增的资源，避免 harness 清空宿主已有状态。
    web_api_registry = registered_web_apis
    context = _build_official_context(Context, config)
    manager = PluginManager(context, config)

    def snapshot_state() -> RuntimeSnapshot:
        pending_tasks = tuple(task for task in asyncio.all_tasks() if not task.done())
        return RuntimeSnapshot(
            handlers=tuple(star_handlers_registry),
            web_apis=tuple(web_api_registry),
            tasks=pending_tasks,
        )

    def snapshot_stars() -> tuple[object, ...]:
        return tuple(context.get_all_stars())

    return LifecycleRuntime(
        plugin_manager=manager,
        snapshot_state=snapshot_state,
        snapshot_stars=snapshot_stars,
    )


def _added(before: Sequence[object], after: Sequence[object]) -> tuple[object, ...]:
    before_ids = {id(item) for item in before}
    return tuple(item for item in after if id(item) not in before_ids)


def _retained(before: Sequence[object], after: Sequence[object]) -> tuple[object, ...]:
    after_ids = {id(item) for item in after}
    return tuple(item for item in before if id(item) in after_ids)


def _resolve_loaded_plugin_name(
    *,
    baseline_stars: Sequence[object],
    current_stars: Sequence[object],
    plugin_dir_name: str,
) -> str:
    """从本轮新增的官方 StarMetadata 中取得管理 API 使用的注册名称。"""

    names: list[str] = []
    for metadata in _added(baseline_stars, current_stars):
        name = getattr(metadata, "name", None)
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(name)

    if len(names) != 1:
        registered_names = ", ".join(repr(name) for name in names) or "无"
        raise LifecycleCheckError(
            "load",
            f"官方 load() 后无法从插件目录 {plugin_dir_name!r} 确定唯一的 "
            f"StarMetadata.name；发现注册名称: {registered_names}",
        )
    return names[0]


def _delta(before: RuntimeSnapshot, after: RuntimeSnapshot) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        handlers=_added(before.handlers, after.handlers),
        web_apis=_added(before.web_apis, after.web_apis),
        tasks=tuple(
            task
            for task in _added(before.tasks, after.tasks)
            if isinstance(task, asyncio.Task) and not task.done()
        ),
    )


def _resource_counts(snapshot: RuntimeSnapshot) -> tuple[int, int, int]:
    return len(snapshot.handlers), len(snapshot.web_apis), len(snapshot.tasks)


def _resource_summary(snapshot: RuntimeSnapshot) -> str:
    handlers, web_apis, tasks = _resource_counts(snapshot)
    return f"handlers={handlers}, web_apis={web_apis}, background_tasks={tasks}"


def _assert_reload_released(
    loaded_resources: RuntimeSnapshot,
    reloaded_state: RuntimeSnapshot,
) -> None:
    retained = RuntimeSnapshot(
        handlers=_retained(loaded_resources.handlers, reloaded_state.handlers),
        web_apis=_retained(loaded_resources.web_apis, reloaded_state.web_apis),
        tasks=_retained(loaded_resources.tasks, reloaded_state.tasks),
    )
    if any(_resource_counts(retained)):
        raise LifecycleCheckError(
            "reload cleanup",
            "官方 reload() 后仍保留首次加载的运行时资源: "
            + _resource_summary(retained),
        )


def _assert_no_runtime_residue(
    baseline: RuntimeSnapshot,
    final: RuntimeSnapshot,
) -> None:
    residue = _delta(baseline, final)
    if any(_resource_counts(residue)):
        raise LifecycleCheckError(
            "resource cleanup",
            "插件卸载后仍存在本轮 lifecycle 新增的运行时资源: "
            + _resource_summary(residue),
        )


def _as_lifecycle_error(phase: str, error: BaseException) -> LifecycleCheckError:
    if isinstance(error, LifecycleCheckError):
        return error
    return LifecycleCheckError(
        phase,
        f"{type(error).__name__}: {error}",
    )


def _format_cleanup_errors(errors: list[tuple[str, BaseException]]) -> str:
    return "; ".join(
        f"{phase}: {type(error).__name__}: {error}" for phase, error in errors
    )


def _require_public_manager_method(
    manager: Any,
    method_name: str,
    phase: str,
) -> Callable[..., Any]:
    method = getattr(manager, method_name, None)
    if not callable(method):
        raise LifecycleCheckError(
            phase,
            f"官方 PluginManager 缺少公开 {method_name}()",
        )
    return method


def _assert_manager_result(
    phase: str,
    method_name: str,
    result: Any,
) -> None:
    if not isinstance(result, tuple) or not result or not bool(result[0]):
        detail = result[1] if isinstance(result, tuple) and len(result) > 1 else None
        raise LifecycleCheckError(
            phase,
            f"官方 PluginManager.{method_name}() 失败: {detail or '未提供错误信息'}",
        )


async def run_lifecycle_check(
    *,
    astrbot_source: str | Path,
    astrbot_version: str,
    plugin_dir: str | Path,
    astrbot_root: str | Path,
    plugin_name: str,
) -> LifecycleReport:
    """执行 load → reload → uninstall → residue check。"""

    source = Path(astrbot_source).expanduser().resolve()
    plugin = Path(plugin_dir).expanduser().resolve()
    root = Path(astrbot_root).expanduser().resolve()
    if not isinstance(astrbot_version, str) or not astrbot_version.strip():
        raise ValueError("AstrBot 版本标识不能为空")
    _validate_plugin_name(plugin_name)
    if not source.is_dir():
        raise ValueError(f"AstrBot 源码目录不存在或不是目录: {source}")

    root_was_present = root.exists()
    if root.is_symlink():
        raise ValueError(f"ASTRBOT_ROOT 不能是符号链接: {root}")
    if root_was_present and not root.is_dir():
        raise ValueError(f"ASTRBOT_ROOT 不是目录: {root}")
    root_was_empty = root_was_present and not any(root.iterdir())

    runtime: LifecycleRuntime | None = None
    baseline: RuntimeSnapshot | None = None
    report: LifecycleReport | None = None
    failure: LifecycleCheckError | None = None
    cleanup_errors: list[tuple[str, BaseException]] = []
    load_succeeded = False
    uninstall_attempted = False
    residue_checked = False
    baseline_stars: tuple[object, ...] | None = None
    registered_plugin_name: str | None = None
    phase = "setup"
    previous_root = os.environ.get("ASTRBOT_ROOT")
    previous_reload = os.environ.get("ASTRBOT_RELOAD")
    inserted_paths: list[str] = []

    try:
        os.environ["ASTRBOT_ROOT"] = str(root)
        os.environ["ASTRBOT_RELOAD"] = "0"

        phase = "staging"
        stage_plugin(
            plugin_dir=plugin,
            astrbot_root=root,
            plugin_name=plugin_name,
        )
        inserted_paths = _prepend_sys_path(root, source)

        phase = "runtime"
        runtime = _build_official_runtime(root, plugin_name)
        baseline_stars = runtime.snapshot_stars()
        baseline = runtime.snapshot_state()

        # load() 使用 staging 目录名；后续公开管理 API 使用 StarMetadata.name。
        phase = "load"
        load = _require_public_manager_method(runtime.plugin_manager, "load", phase)
        _assert_manager_result(
            phase,
            "load",
            await load(specified_dir_name=plugin_name),
        )
        load_succeeded = True
        registered_plugin_name = _resolve_loaded_plugin_name(
            baseline_stars=baseline_stars,
            current_stars=runtime.snapshot_stars(),
            plugin_dir_name=plugin_name,
        )
        loaded_resources = _delta(baseline, runtime.snapshot_state())

        # 官方 reload() 负责 terminate → unbind → load，避免 harness 复制内部流程。
        phase = "reload"
        reload = _require_public_manager_method(runtime.plugin_manager, "reload", phase)
        _assert_manager_result(
            phase,
            "reload",
            await reload(specified_plugin_name=registered_plugin_name),
        )
        reloaded_resources = _delta(baseline, runtime.snapshot_state())
        _assert_reload_released(loaded_resources, reloaded_resources)

        # 官方 uninstall_plugin() 再次通过公开入口终止并解绑最终加载的实例。
        phase = "uninstall"
        uninstall_attempted = True
        uninstall = _require_public_manager_method(
            runtime.plugin_manager,
            "uninstall_plugin",
            phase,
        )
        await uninstall(registered_plugin_name)

        await asyncio.sleep(0)
        phase = "resource cleanup"
        _assert_no_runtime_residue(baseline, runtime.snapshot_state())
        residue_checked = True
        report = LifecycleReport(
            astrbot_version=astrbot_version,
            plugin_name=plugin_name,
            loaded_resource_counts=_resource_counts(loaded_resources),
            reloaded_resource_counts=_resource_counts(reloaded_resources),
        )
    except _CATCHABLE_ERRORS as error:
        failure = _as_lifecycle_error(phase, error)
    finally:
        if runtime is not None and load_succeeded and not uninstall_attempted:
            try:
                uninstall = _require_public_manager_method(
                    runtime.plugin_manager,
                    "uninstall_plugin",
                    "cleanup uninstall",
                )
                uninstall_attempted = True
                if registered_plugin_name is None:
                    raise LifecycleCheckError(
                        "cleanup uninstall",
                        "官方 load() 已成功，但无法确定 StarMetadata.name；"
                        "拒绝使用插件目录名执行卸载。",
                    )
                await uninstall(registered_plugin_name)
            except _CATCHABLE_ERRORS as error:
                cleanup_errors.append(("uninstall", error))

        if runtime is not None and baseline is not None and not residue_checked:
            try:
                await asyncio.sleep(0)
                _assert_no_runtime_residue(baseline, runtime.snapshot_state())
            except _CATCHABLE_ERRORS as error:
                cleanup_errors.append(("resource cleanup", error))

        _remove_sys_path(inserted_paths)
        if previous_root is None:
            os.environ.pop("ASTRBOT_ROOT", None)
        else:
            os.environ["ASTRBOT_ROOT"] = previous_root
        if previous_reload is None:
            os.environ.pop("ASTRBOT_RELOAD", None)
        else:
            os.environ["ASTRBOT_RELOAD"] = previous_reload

        if root.exists() and (root_was_empty or not root_was_present):
            try:
                if root_was_empty:
                    _clear_directory_contents(root)
                else:
                    shutil.rmtree(root)
            except _CATCHABLE_ERRORS as error:
                cleanup_errors.append(("temporary root cleanup", error))

    if failure is not None:
        if cleanup_errors:
            raise LifecycleCheckError(
                failure.phase,
                f"{failure}; cleanup failures: {_format_cleanup_errors(cleanup_errors)}",
            ) from failure
        raise failure
    if cleanup_errors:
        phase, error = cleanup_errors[0]
        raise LifecycleCheckError(
            phase,
            f"{type(error).__name__}: {error}; "
            f"additional cleanup failures: {_format_cleanup_errors(cleanup_errors[1:])}"
            if len(cleanup_errors) > 1
            else f"{type(error).__name__}: {error}",
        ) from error
    if report is None:
        raise LifecycleCheckError("unknown", "lifecycle 未生成检查报告")

    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用官方公开 PluginManager API 检查插件生命周期",
    )
    parser.add_argument("--astrbot-source", required=True, help="官方 AstrBot 源码目录")
    parser.add_argument(
        "--astrbot-version", required=True, help="被测 AstrBot 版本或 ref"
    )
    parser.add_argument("--plugin-dir", required=True, help="当前插件源码目录")
    parser.add_argument("--astrbot-root", required=True, help="临时 ASTRBOT_ROOT")
    parser.add_argument("--plugin-name", required=True, help="插件目录名")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    plugin_dir = Path(args.plugin_dir).expanduser().resolve()
    try:
        report = asyncio.run(
            run_lifecycle_check(
                astrbot_source=args.astrbot_source,
                astrbot_version=args.astrbot_version,
                plugin_dir=plugin_dir,
                astrbot_root=args.astrbot_root,
                plugin_name=args.plugin_name,
            )
        )
    except _CATCHABLE_ERRORS as error:
        print(
            f"{error}\n{traceback.format_exc()}",
            file=sys.stderr,
        )
        return 1

    loaded = report.loaded_resource_counts
    reloaded = report.reloaded_resource_counts
    print(
        "AstrBot plugin lifecycle passed: "
        f"astrbot={report.astrbot_version}, "
        f"plugin={report.plugin_name}, "
        f"loaded(handlers={loaded[0]}, web_apis={loaded[1]}, tasks={loaded[2]}), "
        f"reloaded(handlers={reloaded[0]}, web_apis={reloaded[1]}, tasks={reloaded[2]}), "
        "reload=ok, uninstall=ok, cleanup=ok"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
