import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from aiocqhttp.exceptions import ActionFailed

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.delivery import DeliveryStatus, send_once, log_delivery_error  # noqa: E402


@pytest.mark.parametrize("path", [
    r"\\fixturehost\private\image.jpg",
    r"C:\private\image.jpg",
    "/private/image.jpg",
    r"\\fixturehost\private folder\image.jpg",
    r"C:\private folder\image.jpg",
    "/private folder/image.jpg",
    "file:///private/image.jpg",
    "file://fixturehost/private/image.jpg",
    "private/image.jpg",
    r"private\image.jpg",
    "./private/image.jpg",
    "../private/image.jpg",
])
def test_exception_paths_are_redacted(path):
    from astrbot_plugin_get_px.pixiv.delivery import _safe_error_text

    text = _safe_error_text(str(PermissionError(13, "Permission denied", path)))
    assert "fixturehost" not in text
    assert "private" not in text
    assert "image.jpg" not in text
    assert "Permission denied" in text
    assert "[path]" in text


@pytest.mark.parametrize("text", [
    "path=/private folder/image 01.jpg status=failed",
    '''file="/home/O'Brien/private file"''',
    '''file='/home/a"b/private file' ''',
    "file=[private/image.jpg]",
    r"file=\/private\/image.jpg",
])
def test_protocol_path_boundaries(text):
    from astrbot_plugin_get_px.pixiv.delivery import _safe_error_text

    result = _safe_error_text(text)
    assert "[path]" in result
    assert not any(part in result for part in ("private", "folder", "image", "Brien", 'b/'))
    if "status=failed" in text:
        assert "status=failed" in result


def test_oversized_error_is_bounded_before_regex_processing():
    import re
    from astrbot_plugin_get_px.pixiv.delivery import _safe_error_text

    substitute = re.sub

    def bounded_sub(pattern, replacement, text):
        assert len(text) <= 2048
        return substitute(pattern, replacement, text)

    with patch("astrbot_plugin_get_px.pixiv.delivery.re.sub", side_effect=bounded_sub):
        assert len(_safe_error_text("." * 100_000)) <= 512


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
async def test_chmod_failure_switches_to_base64_and_logs(tmp_path):
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
        assert await send_once(event, [Image.fromFileSystem(str(path))], context="test") is DeliveryStatus.CONFIRMED
    event.bot.call_action.assert_awaited_once()
    event.send.assert_not_awaited()
    logs = " ".join(call.args[0] for call in logger.warning.call_args_list)
    assert 'error_type="PermissionError"' in logs
    assert 'phase=build' in logs and 'to_transport=base64' in logs


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
@pytest.mark.parametrize("forward_transport", ["file", "astrbot"])
@pytest.mark.parametrize("outcome", ["success", "empty", "timeout", "file_error", "retry_error", "rejected"])
async def test_automatic_onebot_delivery(tmp_path, group, forward, forward_transport, outcome):
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image, Plain, Node, Nodes

    path = tmp_path / "fixture.png"
    PILImage.new("RGB", (8, 8)).save(path)
    content = [Plain("fixture"), Image.fromFileSystem(str(path))]
    if forward:
        content = [Nodes([Node(uin="123", name="fixture", content=content)])]
    missing = ActionFailed({"status": "failed", "retcode": 1200, "message": "ENOENT copyfile /shared/timeout/image.jpg"})
    timeout = ActionFailed({"status": "failed", "retcode": 1200, "message": "Timeout: NTEvent NodeIKernelMsgService/sendMsg"})
    side_effect = {
        "success": [dict(message_id=1)], "empty": [None],
        "timeout": [timeout], "file_error": [missing, None],
        "retry_error": [missing, RuntimeError("base64 rejected")],
        "rejected": [ActionFailed({"status": "failed", "retcode": 1200, "message": "res_id failed"})],
    }[outcome]
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456" if group else "",
        get_sender_id=lambda: "789", get_self_id=lambda: "123",
        bot=SimpleNamespace(call_action=AsyncMock(side_effect=side_effect)), send=AsyncMock(),
    )
    framework = forward and forward_transport == "astrbot"
    if framework:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

        event.chain_result = lambda chain: SimpleNamespace(chain=chain)

        framework_sender = getattr(AiocqhttpMessageEvent, "_send", None) or AiocqhttpMessageEvent.send_message

        async def framework_send(chain):
            await framework_sender(
                event.bot, chain, is_group=group, session_id="456" if group else "789"
            )
        event.send.side_effect = framework_send
    expected = (DeliveryStatus.TIMED_OUT if outcome == "timeout" else
                DeliveryStatus.ACCEPTED if outcome == "empty" and not framework else
                DeliveryStatus.UNKNOWN if outcome in ("retry_error", "rejected") or (framework and outcome == "file_error") else DeliveryStatus.CONFIRMED)
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, content, context="test", forward_transport=forward_transport) is expected
    assert event.send.await_count == (1 if framework else 0)
    assert event.bot.call_action.await_count == (2 if not framework and outcome in ("file_error", "retry_error") else 1)
    action = ("send_group" if group else "send_private") + ("_forward_msg" if forward else "_msg")
    for index, call in enumerate(event.bot.call_action.await_args_list):
        args, params = call
        assert args == (action,)
        if not framework:
            assert params["self_id"] == "123"
        segments = params["messages"] if forward else params["message"]
        if forward:
            segments = segments[0]["data"]["content"]
        file = segments[1]["data"]["file"]
        assert file.startswith("base64://") if framework or index else file == path.resolve().as_uri()
    if outcome == "empty" and not framework:
        info = " ".join(call.args[0] for call in logger.info.call_args_list)
        assert "delivery_status=accepted" in info
        assert "delivery_status=confirmed" not in info
    logs = " ".join(call.args[0] for call in logger.warning.call_args_list)
    if outcome in ("file_error", "retry_error"):
        assert "ENOENT copyfile" in logs
        assert ("to_transport=base64" in logs) is not framework
    if outcome == "retry_error" and not framework:
        assert "base64 rejected" in logs and "transport=base64" in logs
    if outcome == "timeout":
        assert "1200" in logs and "sendMsg" in logs and "delivery_status=timed_out retry=no" in logs


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), ActionFailed({"retcode": 1200, "message": "upload timeout"})])
async def test_framework_timeout_is_logged_without_notice_or_resend(error):
    event = SimpleNamespace(chain_result=lambda content: content, send=AsyncMock(side_effect=error))
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, ["image"], context="test") is DeliveryStatus.TIMED_OUT
    event.send.assert_awaited_once_with(["image"])
    assert "delivery_status=timed_out retry=no" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_build_failure_only_logs():
    event = SimpleNamespace(chain_result=Mock(side_effect=ValueError("invalid chain")), send=AsyncMock())
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, [], context="test") is DeliveryStatus.FAILED
    event.send.assert_not_awaited()
    assert "invalid chain" in logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_cancel_propagates_without_retry():
    event = SimpleNamespace(chain_result=lambda content: content, send=AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await send_once(event, [], context="test")
    event.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_local_file_logs_both_build_attempts(tmp_path):
    from astrbot.api.message_components import Image
    event = SimpleNamespace(get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456",
                            get_sender_id=lambda: "789", bot=SimpleNamespace(call_action=AsyncMock()), send=AsyncMock())
    with patch("astrbot_plugin_get_px.pixiv.delivery.logger") as logger:
        assert await send_once(event, [Image.fromFileSystem(str(tmp_path / "missing.png"))], context="test") is DeliveryStatus.FAILED
    event.bot.call_action.assert_not_awaited()
    event.send.assert_not_awaited()
    logs = " ".join(call.args[0] for call in logger.warning.call_args_list)
    assert "transport=file" in logs and "transport=base64" in logs and "to_transport=base64" in logs


def test_protocol_error_details_are_allowlisted_and_redacted():
    from astrbot_plugin_get_px.pixiv.delivery import _safe_error_text

    diagnostic = (
        "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
        "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet:\n{}"
    )
    assert _safe_error_text(diagnostic) == diagnostic
    error = ActionFailed(
        {
            "status": "failed",
            "retcode": 1200,
            "message": diagnostic + ' upload failed token=secret-value "api_key": "json secret" https://example.com/private?key=hidden',
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
    assert "NodeIKernelMsgService/sendMsg" in text
    assert "NodeIKernelMsgListener/onMsgInfoListUpdate" in text
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


@pytest.mark.parametrize("message,expected", [
    ("Timeout: NTEvent sendMsg ENOENT copyfile", True),
    ("retcode=1200 res_id failed", False),
    ("operation timed out", True),
    ("path=/shared/image.jpg operation timed out", True),
    ("file=/tmp/x.jpg: operation timed out", True),
    ("file=/tmp/abc operation timed out", True),
    ("file=/tmp/x.jpg 上传超时", True),
    ("path=/tmp/x.jpg 读取超时", True),
    ("file=/tmp/image folder/timeout.jpg", False),
    ("file=/tmp/image folder/上传超时.png", False),
    ("x" * 2100 + " operation timed out", True),
    (r"ENOENT copyfile C:\shared\timeout\image.jpg", False),
    ("ENOENT copyfile /shared/timeout/image.jpg", False),
    ("ENOENT copyfile /shared/超时/image.jpg", False),
])
def test_timeout_classification_includes_wrapped_cause(message, expected):
    from astrbot_plugin_get_px.pixiv.delivery import _is_timeout
    cause = RuntimeError(message)
    error = RuntimeError("send failed")
    error.__cause__ = cause
    assert _is_timeout(error) is expected
    # 同一规则用于日志；路径应隐藏，路径后的诊断应保留。
    from astrbot_plugin_get_px.pixiv.delivery import _safe_error_text
    safe = _safe_error_text(message)
    if "file=/tmp/" in message or "path=/tmp/" in message:
        assert "/tmp/" not in safe and "[path]" in safe
        if expected:
            assert "timed out" in safe or "超时" in safe


def test_file_error_classification_ignores_paths_and_unrelated_response_fields():
    from astrbot_plugin_get_px.pixiv.delivery import _is_file_read_error

    for message in ("send failed path=/shared/copyfile/image.jpg", "send failed"):
        error = ActionFailed({"message": message, "data": {"path": "/shared/copyfile/image.jpg"}})
        assert not _is_file_read_error(error)
    assert _is_file_read_error(ActionFailed({"message": "ENOENT copyfile /shared/image.jpg"}))


@pytest.mark.asyncio
async def test_file_receipt_validation_retains_malformed_ids(tmp_path):
    from PIL import Image as PILImage
    from astrbot.api.message_components import Image

    path = tmp_path / "image.png"
    PILImage.new("RGB", (8, 8)).save(path)
    event = SimpleNamespace(
        get_platform_name=lambda: "aiocqhttp", get_group_id=lambda: "456",
        get_sender_id=lambda: "789", bot=SimpleNamespace(call_action=AsyncMock()),
    )
    for value, expected in ((True, DeliveryStatus.ACCEPTED), ({"id": 1}, DeliveryStatus.ACCEPTED),
                            ("   ", DeliveryStatus.ACCEPTED), ("123", DeliveryStatus.CONFIRMED)):
        event.bot.call_action.return_value = {"message_id": value}
        assert await send_once(event, [Image.fromFileSystem(str(path))], context="receipt_test") is expected
