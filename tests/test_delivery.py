import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiocqhttp.exceptions import ActionFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus, send_once, log_delivery_error  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["file", "base64"])
@pytest.mark.parametrize("platform", ["posix", "nt"])
async def test_file_sharing_permissions_are_posix_file_only(tmp_path, platform, transport):
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image, Node, Nodes
    from astrbot_plugin_get_px.pixiv import delivery

    path = tmp_path / "download.png"
    PILImage.new("RGB", (8, 8)).save(path)
    content = [Nodes([Node(uin="123", name="fixture", content=[Image.fromFileSystem(str(path))])])]
    chmod = Mock()
    with patch.object(delivery, "os", SimpleNamespace(name=platform, chmod=chmod)):
        segments = await delivery._onebot_segments(content, transport)
    assert segments[0]["data"]["content"][0]["type"] == "image"
    if platform == "posix" and transport == "file":
        chmod.assert_called_once_with(path, 0o644)
    else:
        chmod.assert_not_called()


@pytest.mark.asyncio
async def test_chmod_failure_is_logged_before_api_call(tmp_path):
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image
    from astrbot_plugin_get_px.pixiv import delivery

    path = tmp_path / "download.png"
    PILImage.new("RGB", (8, 8)).save(path)
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456",
        get_sender_id=lambda: "789", bot=SimpleNamespace(call_action=AsyncMock()),
        send=AsyncMock(), plain_result=lambda text: text,
    )
    with patch.object(delivery, "os", SimpleNamespace(name="posix", chmod=Mock(side_effect=PermissionError("denied")))), patch.object(delivery, "logger") as logger:
        assert await send_once(event, [Image.fromFileSystem(str(path))], context="test") is DeliveryStatus.FAILED
    event.bot.call_action.assert_not_awaited()
    event.send.assert_awaited_once()
    assert "构建失败" in event.send.await_args.args[0]
    assert 'error_type="PermissionError"' in logger.warning.call_args.args[0]
    assert 'phase=build' in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_real_posix_mkstemp_image_becomes_cross_user_readable(tmp_path):
    import os
    import stat
    import tempfile
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image
    from astrbot_plugin_get_px.pixiv.delivery import _onebot_segments

    if os.name != "posix":
        pytest.skip("requires POSIX permission bits")
    fd, name = tempfile.mkstemp(dir=tmp_path, suffix=".png")
    with os.fdopen(fd, "wb") as stream:
        PILImage.new("RGB", (8, 8)).save(stream, format="PNG")
    path = Path(name)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    await _onebot_segments([Image.fromFileSystem(name)], "file")
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


@pytest.mark.asyncio
@pytest.mark.parametrize("group", [True, False])
@pytest.mark.parametrize("forward", [True, False])
@pytest.mark.parametrize("transport", ["file", "base64", "astrbot"])
async def test_direct_onebot_transport(tmp_path, group, forward, transport):
    import base64
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image, Plain, Node, Nodes

    path = tmp_path / "fixture.png"
    PILImage.new("RGB", (32, 32), "blue").save(path)
    content = [Plain("fixture"), Image.fromFileSystem(str(path))]
    if forward:
        content = [Nodes([Node(uin="123", name="fixture", content=content)])]
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456" if group else "",
        get_sender_id=lambda: "789", get_self_id=lambda: "123",
        bot=SimpleNamespace(call_action=AsyncMock(return_value={"message_id": 1})),
        send=AsyncMock(), chain_result=lambda items: items,
    )
    assert await send_once(event, content, context="test", onebot_transport=transport) is DeliveryStatus.CONFIRMED
    if transport == "astrbot":
        event.bot.call_action.assert_not_awaited()
        event.send.assert_awaited_once()
        return
    event.send.assert_not_awaited()
    action = ("send_group" if group else "send_private") + ("_forward_msg" if forward else "_msg")
    args, params = event.bot.call_action.await_args
    assert args == (action,)
    assert params["group_id" if group else "user_id"] == (456 if group else 789)
    assert params["self_id"] == "123"
    segments = params["messages" if forward else "message"]
    if forward:
        segments = segments[0]["data"]["content"]
    file = segments[1]["data"]["file"]
    assert (file == path.resolve().as_uri()) if transport == "file" else (base64.b64decode(file[9:]) == path.read_bytes())


@pytest.mark.asyncio
async def test_direct_onebot_timeout_never_falls_back(tmp_path):
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image
    path = tmp_path / "fixture.png"
    PILImage.new("RGB", (8, 8)).save(path)
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456",
        get_sender_id=lambda: "789", bot=SimpleNamespace(call_action=AsyncMock(side_effect=TimeoutError())),
        send=AsyncMock(), plain_result=lambda text: text,
    )
    assert await send_once(event, [Image.fromFileSystem(str(path))], context="test") is DeliveryStatus.UNKNOWN
    event.bot.call_action.assert_awaited_once()
    assert event.send.await_count == 1  # 仅状态提示，不补发图片。


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
        assert await send_once(event, ["image"], context="send_mode=single") is DeliveryStatus.UNKNOWN
    assert delivered == [["image"]]
    assert "delivery_status=unknown" in logger.warning.call_args.args[0]
    assert "retcode=" in logger.warning.call_args.args[0]
    assert "请先查看聊天记录" in event.send.await_args.args[0]


@pytest.mark.asyncio
async def test_pre_send_failure_only_sends_failure_notice():
    event = SimpleNamespace(
        chain_result=Mock(side_effect=ValueError("invalid chain")), send=AsyncMock(),
        plain_result=lambda text: text,
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, [], context="send_mode=single") is DeliveryStatus.FAILED
    event.send.assert_awaited_once()
    assert "构建失败" in event.send.await_args.args[0]
    assert "delivery_status=failed" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_notice_error_is_logged_and_not_retried():
    event = SimpleNamespace(
        chain_result=lambda content: content,
        send=AsyncMock(side_effect=TimeoutError()),
        plain_result=lambda text: text,
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, [], context="send_mode=forward") is DeliveryStatus.UNKNOWN
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"status": "ok", "retcode": 0, "data": {"message_id": 123}}, DeliveryStatus.CONFIRMED),
        ({"status": "async", "retcode": 1, "data": None}, DeliveryStatus.UNKNOWN),
        ({"status": "ok", "retcode": 0, "data": {}}, DeliveryStatus.UNKNOWN),
        ({"status": "failed", "retcode": 1200, "data": None}, DeliveryStatus.UNKNOWN),
    ],
)
async def test_direct_receipts_use_sdk_contract_and_require_message_id(payload, expected):
    from aiocqhttp.api_impl import _handle_api_result
    from astrbot.api.message_components import Plain

    async def action(*_args, **_kwargs):
        return _handle_api_result(payload)

    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456",
        get_sender_id=lambda: "789", bot=SimpleNamespace(call_action=AsyncMock(side_effect=action)),
        send=AsyncMock(), plain_result=lambda text: text,
    )
    assert await send_once(event, [Plain("fixture")], context="test") is expected
    event.bot.call_action.assert_awaited_once()
    assert event.send.await_count == (0 if expected is DeliveryStatus.CONFIRMED else 1)


@pytest.mark.asyncio
async def test_build_failure_notice_error_is_logged_without_image_submission():
    event = SimpleNamespace(
        chain_result=Mock(side_effect=ValueError("invalid chain")),
        send=AsyncMock(side_effect=RuntimeError("notice failed")),
        plain_result=lambda text: text,
    )
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, [], context="test") is DeliveryStatus.FAILED
    event.send.assert_awaited_once()
    assert len(logger.warning.call_args_list) == 2
    assert "phase=notice" in logger.warning.call_args.args[0]


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
