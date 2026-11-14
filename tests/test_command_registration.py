import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.filter.regex import RegexFilter
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from astrbot_plugin_get_px import main


class _CheckinHelpEvent:
    def __init__(self) -> None:
        self.stopped = False

    def stop_event(self) -> None:
        self.stopped = True

    def plain_result(self, text: str):
        return text

    def chain_result(self, chain: list):
        return chain


async def _collect(async_iterable):
    return [item async for item in async_iterable]


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


def test_checkin_commands_are_grouped_as_flat_command_groups() -> None:
    paths = _registered_command_paths()

    assert all(not path.startswith("/") for path in paths)
    assert {"p", "签到", "签到帮助", "刷新背景"} <= paths
    assert {
        "签到状态",
        "签到成就",
        "签到生日 查看",
        "签到生日 设置",
        "签到生日 清除",
        "签到称号 查看",
        "签到称号 佩戴",
        "签到排行 今日",
        "签到排行 月榜",
        "签到排行 连签",
        "签到排行 累计",
        "签到商店 查看",
        "签到商店 加持",
        "签到主题 列表",
        "签到主题 查看",
        "签到主题 购买",
        "签到主题 切换",
        "签到管理 预览",
        "签到管理 导出",
        "签到管理 事件查看",
        "签到管理 事件添加",
        "签到管理 事件删除",
    } <= paths

    # 旧路径与旧组全部移除
    assert not {"签到中心"} & paths
    assert not {"签到我的 " + suffix for suffix in ("状态", "成就", "生日查看", "生日设置", "生日清除", "称号查看", "称号佩戴")} & paths
    assert not {"签到商店 主题列表", "签到商店 主题查看", "签到商店 主题购买", "签到商店 主题切换", "签到商店 刷新背景"} & paths


def test_checkin_command_groups_expose_focused_groups() -> None:
    group_names = {
        handler.handler_name
        for handler in _plugin_command_handlers()
        if any(
            isinstance(event_filter, CommandGroupFilter)
            for event_filter in handler.event_filters
        )
    }
    assert {
        "checkin_birthday",
        "checkin_title",
        "checkin_ranking",
        "checkin_shop",
        "checkin_theme",
        "checkin_admin",
    } <= group_names
    assert "checkin_my" not in group_names


def test_checkin_calendar_is_a_top_level_command_and_delegates_month() -> None:
    paths = _registered_command_paths()
    assert "签到日历" in paths

    plugin = object.__new__(main.GetPxPlugin)
    calls: list[tuple[str, ...]] = []

    async def _fake_handler(event, month: str = ""):
        calls.append((month,))
        yield event.plain_result("ok")

    plugin._handle_checkin_calendar = _fake_handler
    event = _CheckinHelpEvent()

    output = asyncio.run(_collect(plugin.cmd_checkin_calendar(event, "2026-08")))

    assert event.stopped
    assert calls == [("2026-08",)]
    assert output == ["ok"]


def test_checkin_help_sends_the_help_image() -> None:
    event = _CheckinHelpEvent()
    plugin = object.__new__(main.GetPxPlugin)

    output = asyncio.run(_collect(plugin.cmd_checkin_help(event)))

    assert event.stopped
    assert len(output) == 1
    assert len(output[0]) == 1
    assert type(output[0][0]).__name__ == "Image"
    assert Path(output[0][0].path) == main.CHECKIN_HELP_IMAGE
    assert main.CHECKIN_HELP_IMAGE.is_file()


def test_checkin_admin_subcommands_keep_admin_permission() -> None:
    admin_handlers = {
        "cmd_checkin_preview",
        "cmd_checkin_export",
        "cmd_checkin_event_list",
        "cmd_checkin_event_add",
        "cmd_checkin_event_delete",
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


def test_only_plain_checkin_uses_regex_trigger() -> None:
    regex_handlers = [
        (handler.handler_name, event_filter)
        for handler in _plugin_command_handlers()
        for event_filter in handler.event_filters
        if isinstance(event_filter, RegexFilter)
    ]
    assert [name for name, _ in regex_handlers] == ["checkin_auto_trigger"]
    regex_filter = regex_handlers[0][1]

    assert regex_filter.regex.fullmatch("签到")
    assert not regex_filter.regex.fullmatch("签到中心")
    assert not regex_filter.regex.fullmatch("签到帮助")

    assert not regex_filter.regex.fullmatch("来一份图")
    assert not regex_filter.regex.fullmatch("来三张初音ミク图")
