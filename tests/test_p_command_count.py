"""Regression tests for /p [tag] [count] parameter parsing."""

import inspect
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from astrbot_plugin_get_px.main import GetPxPlugin


def _cmd_p_filter() -> CommandFilter:
    for handler in star_handlers_registry.get_handlers_by_event_type(
        EventType.AdapterMessageEvent
    ):
        if "astrbot_plugin_get_px" not in str(handler.handler_module_path):
            continue
        if handler.handler_name != "cmd_p":
            continue
        for event_filter in handler.event_filters:
            if isinstance(event_filter, CommandFilter):
                return event_filter
    raise AssertionError("cmd_p CommandFilter not registered")


def test_cmd_p_registers_greedy_str_query() -> None:
    """AstrBot only treats a param as greedy when default/annotation is GreedyStr itself."""
    cmd_filter = _cmd_p_filter()
    assert "query" in cmd_filter.handler_params
    assert cmd_filter.handler_params["query"] is GreedyStr
    assert inspect.signature(GetPxPlugin.cmd_p).parameters["query"].default is GreedyStr


def test_cmd_p_greedy_str_keeps_trailing_count() -> None:
    """/p 标签 3 must keep both tokens so _split_tag_and_count can read the count."""
    cmd_filter = _cmd_p_filter()
    params = cmd_filter.validate_and_convert_params(
        ["初音ミク", "3"], cmd_filter.handler_params
    )
    assert params["query"] == "初音ミク 3"

    empty = cmd_filter.validate_and_convert_params([], cmd_filter.handler_params)
    assert empty["query"] == ""

    only_count = cmd_filter.validate_and_convert_params(
        ["2"], cmd_filter.handler_params
    )
    assert only_count["query"] == "2"


def test_split_tag_and_count_extracts_trailing_number() -> None:
    assert GetPxPlugin._split_tag_and_count("") == ("", "")
    assert GetPxPlugin._split_tag_and_count("3") == ("", "3")
    assert GetPxPlugin._split_tag_and_count("初音ミク 3") == ("初音ミク", "3")
    assert GetPxPlugin._split_tag_and_count("初音 ミク 3") == ("初音 ミク", "3")
    assert GetPxPlugin._split_tag_and_count("初音ミク") == ("初音ミク", "")
    # non-separated trailing digit is part of the tag, not count
    assert GetPxPlugin._split_tag_and_count("初音3") == ("初音3", "")
