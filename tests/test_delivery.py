import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiocqhttp.exceptions import ActionFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.delivery import send_once, log_delivery_error  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ActionFailed(
            {"status": "failed", "retcode": 1200, "wording": "upload timeout"}
        ),
        TimeoutError(),
    ],
)
async def test_delivered_then_error_does_not_resend_image(error):
    delivered = []

    async def send(message):
        if isinstance(message, list):
            delivered.append(message)
            raise error

    event = SimpleNamespace(
        chain_result=lambda content: content,
        send=AsyncMock(side_effect=send),
        plain_result=lambda text: text,
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert not await send_once(event, ["image"], context="send_mode=single")
    assert delivered == [["image"]]
    assert "delivery_status=unknown" in logger.warning.call_args.args[0]
    assert "retcode=" in logger.warning.call_args.args[0]
    assert "请先查看聊天记录" in event.send.await_args.args[0]


@pytest.mark.asyncio
async def test_pre_send_failure_never_calls_send():
    event = SimpleNamespace(
        chain_result=Mock(side_effect=ValueError("invalid chain")), send=AsyncMock()
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert not await send_once(event, [], context="send_mode=single")
    event.send.assert_not_awaited()
    assert "delivery_status=failed" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_notice_error_is_logged_and_not_retried():
    event = SimpleNamespace(
        chain_result=lambda content: content,
        send=AsyncMock(side_effect=TimeoutError()),
        plain_result=lambda text: text,
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert not await send_once(event, [], context="send_mode=forward")
    assert event.send.await_count == 2  # 图片一次，状态提示一次。
    assert "phase=notice" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_cancel_propagates_without_retry():
    event = SimpleNamespace(
        chain_result=lambda content: content,
        send=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    with pytest.raises(asyncio.CancelledError):
        await send_once(event, [], context="send_mode=single")
    event.send.assert_awaited_once()


def test_protocol_error_details_are_allowlisted_and_redacted():
    error = ActionFailed(
        {
            "status": "failed",
            "retcode": 1200,
            "message": 'upload failed token=secret-value "api_key": "json secret" https://example.com/private?key=hidden',
            "wording": "user 123456789 C:\\private\\image.jpg\nfailed",
            "params": {"message": "PRIVATE CONTENT"},
        }
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        log_delivery_error(
            error, context="send_mode=single", phase="send", delivery_status="unknown"
        )
    text = logger.warning.call_args.args[0]
    assert "1200" in text and "upload failed" in text
    for secret in (
        "secret-value",
        "json secret",
        "example.com",
        "hidden",
        "123456789",
        "private",
        "PRIVATE CONTENT",
        "\n",
    ):
        assert secret not in text
