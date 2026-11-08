"""Runtime loggers come from AstrBot; wording must not overstate outcomes."""

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus, send_once
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
from astrbot_plugin_get_px.checkin.omnidraw_bridge import OmnidrawBridge

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "main.py", ROOT / "group_safety.py"] + [
    p
    for directory in ("pixiv", "checkin", "plugin_api")
    for p in (ROOT / directory).rglob("*.py")
]


def test_runtime_logger_imports_are_astrbot_only():
    for path in FILES:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        uses_logger = False
        imports_logger = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "logging" for alias in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert node.module != "logging", path
                if any(alias.name == "logger" for alias in node.names):
                    assert node.module == "astrbot.api", path
                    imports_logger = True
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
            ):
                uses_logger |= node.func.value.id == "logger"
        assert not uses_logger or imports_logger, path
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for handler in node.handlers:
                assert not any(
                    isinstance(child, ast.Name)
                    and child.id == "logger"
                    and isinstance(child.ctx, ast.Store)
                    for child in ast.walk(handler)
                ), path


@pytest.mark.asyncio
async def test_empty_receipt_log_reports_call_return_not_delivery():
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp",
        bot=SimpleNamespace(call_action=AsyncMock(return_value={})),
    )
    with (
        patch(
            "astrbot_plugin_get_px.pixiv.delivery._onebot_request",
            AsyncMock(return_value=("send_group_msg", {})),
        ),
        patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger,
    ):
        assert (
            await send_once(event, [], context="feature=test")
            is DeliveryStatus.ACCEPTED
        )
    text = logger.info.call_args.args[0]
    assert "发送调用返回" in text and "delivery_status=accepted" in text
    assert "成功" not in text and "已送达" not in text


@pytest.mark.asyncio
async def test_private_policy_failure_log_names_session_not_group():
    source = object.__new__(FiltersMixin)
    source.group_safety_service = SimpleNamespace(
        get_private_policy=AsyncMock(side_effect=RuntimeError("secret-private-value"))
    )
    event = SimpleNamespace(get_group_id=lambda: "", get_sender_id=lambda: "1")
    with patch("astrbot_plugin_get_px.pixiv.filters.logger") as logger:
        policy = await source._content_safety_policy(event)
    text = logger.error.call_args.args[0]
    assert policy.general_only_enabled and "会话安全策略读取失败" in text
    assert "scope=private" in text
    assert "error_type=RuntimeError" in text and "secret-private-value" not in text


def test_index_on_access_cleanup_does_not_claim_a_scheduler(tmp_path):
    index = ImageIndexStore(tmp_path)
    index._cleanup_old_days_sync = Mock(return_value=1)
    index._last_cleanup_ts = 0
    try:
        with patch("astrbot_plugin_get_px.pixiv.index.logger") as logger:
            index._maybe_cleanup_old_days_sync(index.today_key())
        assert "trigger=access" in logger.info.call_args.args[0]
        assert "定时" not in logger.info.call_args.args[0]
    finally:
        index.close()


def test_omnidraw_error_log_only_exposes_allowlisted_member():
    bridge = OmnidrawBridge(SimpleNamespace())
    with patch("astrbot_plugin_get_px.checkin.omnidraw_bridge.logger") as logger:
        bridge._incompatible(AttributeError("missing _usage_lock secret-token-value"))
    text = logger.info.call_args.args[0]
    assert "member=_usage_lock" in text and "error_type=AttributeError" in text
    assert "secret-token-value" not in text and "版本过旧" not in text
