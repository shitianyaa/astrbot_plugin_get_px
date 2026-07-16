from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from astrbot_plugin_get_px import main


def _plugin_command_handlers():
    return [
        handler
        for handler in star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
        if "astrbot_plugin_get_px" in str(handler.handler_module_path)
    ]


def _registered_command_paths() -> set[str]:
    paths: set[str] = set()
    for handler in _plugin_command_handlers():
        for event_filter in handler.event_filters:
            if isinstance(event_filter, (CommandFilter, CommandGroupFilter)):
                paths.update(event_filter.get_complete_command_names())
    return paths


def test_checkin_commands_are_grouped_under_command_center() -> None:
    paths = _registered_command_paths()

    assert all(not path.startswith("/") for path in paths)
    assert {"p", "签到", "签到中心"} <= paths
    assert {
        "签到中心 我的 状态",
        "签到中心 我的 生日",
        "签到中心 我的 成就",
        "签到中心 我的 称号 查看",
        "签到中心 我的 称号 佩戴",
        "签到中心 排行",
        "签到中心 商店 查看",
        "签到中心 商店 加持",
        "签到中心 商店 刷新背景",
        "签到中心 商店 主题 列表",
        "签到中心 商店 主题 查看",
        "签到中心 商店 主题 购买",
        "签到中心 商店 主题 切换",
        "签到中心 管理 预览",
        "签到中心 管理 导出",
        "签到中心 管理 事件",
    } <= paths

    assert not {
        "签到帮助",
        "签到状态",
        "签到排行",
        "签到商店",
        "购买加持",
        "签到主题",
        "查看主题",
        "购买主题",
        "切换主题",
        "刷新签到背景",
        "签到生日",
        "签到成就",
        "签到称号",
        "佩戴称号",
        "签到测试",
        "签到导出",
        "签到事件",
    } & paths


def test_checkin_command_center_exposes_four_sections() -> None:
    root_filter = next(
        event_filter
        for handler in _plugin_command_handlers()
        if handler.handler_name == "checkin_center"
        for event_filter in handler.event_filters
        if isinstance(event_filter, CommandGroupFilter)
    )

    tree = root_filter.print_cmd_tree(root_filter.sub_command_filters)
    assert "├── 我的" in tree
    assert "├── 排行" in tree
    assert "├── 商店" in tree
    assert "├── 管理" in tree


def test_checkin_admin_subcommands_keep_admin_permission() -> None:
    admin_handlers = {
        "cmd_checkin_preview",
        "cmd_checkin_export",
        "cmd_checkin_event_admin",
    }
    handlers = {
        handler.handler_name: handler for handler in _plugin_command_handlers()
    }

    for handler_name in admin_handlers:
        assert any(
            isinstance(event_filter, PermissionTypeFilter)
            and event_filter.permission_type == PermissionType.ADMIN
            for event_filter in handlers[handler_name].event_filters
        )


def test_plain_checkin_trigger_is_preserved() -> None:
    regex_filter = next(
        event_filter
        for handler in _plugin_command_handlers()
        if handler.handler_name == "checkin_auto_trigger"
        for event_filter in handler.event_filters
        if isinstance(event_filter, RegexFilter)
    )

    assert regex_filter.regex.fullmatch("签到")
    assert not regex_filter.regex.fullmatch("签到中心")


def test_static_checkin_help_command_and_assets_are_removed() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not hasattr(main, "CHECKIN_HELP_IMAGE")
    assert not hasattr(main.GetPxPlugin, "cmd_checkin_help")
    assert not (root / "assets/checkin_help.png").exists()
    assert not (root / "assets/checkin_help.html").exists()
