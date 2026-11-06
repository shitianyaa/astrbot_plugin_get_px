from __future__ import annotations

import asyncio
import json
import os
import re
import time
from enum import Enum
from pathlib import Path

from astrbot.api import logger
from astrbot.api.message_components import Image, Plain, Node, Nodes


class DeliveryStatus(Enum):
    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DeliveryNotSubmitted(asyncio.CancelledError):
    """消息尚未提交时取消；调用方应释放图片占用并继续传播取消。"""


async def _onebot_segments(content: list, transport: str) -> list[dict]:
    """仅转换发图链路使用的组件，路径模式不经过框架的 base64 转换。"""
    segments = []
    for component in content:
        if isinstance(component, Plain):
            segments.append({"type": "text", "data": {"text": component.text}})
        elif isinstance(component, Image):
            if transport == "base64":
                file = "base64://" + await component.convert_to_base64()
            else:
                path = Path(component.path)
                if not component.path or not path.is_file():
                    raise ValueError("OneBot 本地图片文件不存在")
                if os.name == "posix":
                    # mkstemp 默认为 0600；共享目录中的协议端可能使用不同 UID。
                    os.chmod(path, 0o644)
                file = path.resolve().as_uri()
            segments.append({"type": "image", "data": {"file": file}})
        elif isinstance(component, Node):
            segments.append(
                {
                    "type": "node",
                    "data": {
                        "name": component.name,
                        "uin": str(component.uin),
                        "content": await _onebot_segments(component.content, transport),
                    },
                }
            )
        elif isinstance(component, Nodes):
            segments.extend(await _onebot_segments(component.nodes, transport))
        else:
            raise ValueError("OneBot 发图组件不受支持")
    return segments


async def _onebot_request(event, content: list, transport: str) -> tuple[str, dict]:
    group_id = str(event.get_group_id() or "")
    target = group_id or str(event.get_sender_id() or "")
    if not target.isdigit():
        raise ValueError("OneBot 会话标识无效")
    forward = any(isinstance(item, (Node, Nodes)) for item in content)
    action = "send_group" if group_id else "send_private"
    action += "_forward_msg" if forward else "_msg"
    params = {"group_id" if group_id else "user_id": int(target)}
    params["messages" if forward else "message"] = await _onebot_segments(
        content, transport
    )
    self_id = getattr(event, "get_self_id", lambda: None)()
    if self_id:
        params["self_id"] = str(self_id)
    return action, params


def _safe_error_text(value: object) -> str:
    """保留诊断文本，移除地址、标识和凭据；限制异常回包撑大日志。"""
    if not isinstance(value, str):
        return ""
    # 输出仅保留 512 字符；先限制输入，避免异常长回包拖慢事件循环。
    text = value[:2048].replace(r"\/", "/")
    text = re.sub(r"https?://\S+|\[CQ:[^\]]*\]", "[redacted]", text)
    path_start = r"(?:file://|\\{2,}|[a-z]:[\\/]|/|\.\.?[\\/])"
    text = re.sub(
        rf"""(?i)"{path_start}[^"\r\n]*"|'{path_start}[^'\r\n]*'""",
        "[path]",
        text,
    )
    text = re.sub(
        rf'''(?i)\b((?:file|path|filename)\s*=\s*){path_start}[^"'\r\n,;]+?(?=\s+\w+\s*[:=]|["'\r\n,;]|$)''',
        r"\1[path]",
        text,
    )
    # 斜杠须位于路径起点，保留 NodeIKernelMsgService/sendMsg 等诊断标识。
    text = re.sub(rf'''(?i)(?<![\w/\\]){path_start}[^\s"']+''', "[path]", text)
    text = re.sub(
        r'''(?i)(?<![\w/\\])(?:[\w.-]+[\\/]+)+[\w.-]+\.[a-z0-9]+(?=$|[\s"',;:)\]])''',
        "[path]",
        text,
    )
    text = re.sub(
        r"""(?i)((?:token|secret|password|authorization|api[_-]?key)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", text)
    text = re.sub(r"\d{5,}", "[id]", text)
    return text[:512]


def log_delivery_error(
    error: Exception,
    *,
    context: str,
    phase: str,
    delivery_status: str,
    duration_ms: int = 0,
) -> None:
    """仅记录允许的协议错误字段，不输出完整请求或回包。"""
    result = getattr(error, "result", None)
    result = result if isinstance(result, dict) else {}
    retcode = result.get("retcode")
    fields = {
        "error_type": type(error).__name__,
        "retcode": retcode if type(retcode) is int else None,
        "status": result.get("status")
        if result.get("status") in ("ok", "failed", "async")
        else None,
        "message": _safe_error_text(result.get("message", "")),
        "wording": _safe_error_text(result.get("wording", "")),
    }
    if not fields["message"] and not fields["wording"]:
        fields["message"] = _safe_error_text(str(error)) if not result else ""
    detail = " ".join(
        f"{key}={json.dumps(value, ensure_ascii=False)}"
        for key, value in fields.items()
    )
    logger.warning(
        f"[GetPx] 发图链路异常: {context} phase={phase} "
        f"delivery_status={delivery_status} duration_ms={duration_ms} {detail}"
    )


async def _send_notice(event, text: str, *, context: str, status: DeliveryStatus) -> None:
    try:
        await event.send(event.plain_result(text))
    except Exception as exc:
        log_delivery_error(
            exc, context=context, phase="notice", delivery_status=status.value
        )


async def send_once(
    event, content: list, *, context: str, onebot_transport: str = "file"
) -> DeliveryStatus:
    """发送接口无幂等保证；异常时保留未知状态，不自动重发图片。"""
    started = time.monotonic()
    direct = (
        onebot_transport in ("file", "base64")
        and getattr(event, "get_platform_name", lambda: "")() == "aiocqhttp"
        and getattr(event, "bot", None) is not None
    )
    context += f" transport={onebot_transport if direct else 'astrbot'}"
    try:
        if direct:
            action, params = await _onebot_request(event, content, onebot_transport)
        else:
            message = event.chain_result(content)
    except asyncio.CancelledError as exc:
        raise DeliveryNotSubmitted() from exc
    except Exception as exc:
        log_delivery_error(
            exc, context=context, phase="build", delivery_status="failed"
        )
        try:
            await _send_notice(
                event, "⚠️ 图片消息构建失败，本次未发送且不扣费，请检查插件日志。",
                context=context, status=DeliveryStatus.FAILED,
            )
        except asyncio.CancelledError as cancelled:
            raise DeliveryNotSubmitted() from cancelled
        return DeliveryStatus.FAILED
    try:
        if direct:
            receipt = await event.bot.call_action(action, **params)
            # aiocqhttp 已解包 data；异步受理可返回 None，不能据此确认发图。
            message_id = receipt.get("message_id") if isinstance(receipt, dict) else None
            if type(message_id) not in (int, str) or not str(message_id).strip():
                logger.warning(
                    f"[GetPx] 发图回执缺少消息标识: {context} "
                    f"phase=receipt delivery_status=unknown"
                )
                await _send_notice(
                    event, "⚠️ 图片送达状态未知，已停止自动重发；请先查看聊天记录，本次未确认部分不扣费。",
                    context=context, status=DeliveryStatus.UNKNOWN,
                )
                return DeliveryStatus.UNKNOWN
        else:
            await event.send(message)
    except Exception as exc:
        log_delivery_error(
            exc,
            context=context,
            phase="send",
            delivery_status="unknown",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        await _send_notice(
            event, "⚠️ 图片送达状态未知，已停止自动重发；请先查看聊天记录，本次未确认部分不扣费。",
            context=context, status=DeliveryStatus.UNKNOWN,
        )
        return DeliveryStatus.UNKNOWN
    logger.info(
        f"[GetPx] 作品发送已确认: {context} delivery_status=confirmed duration_ms={int((time.monotonic() - started) * 1000)}"
    )
    return DeliveryStatus.CONFIRMED


class DeliveryMixin:
    """提供发送失败时的用户友好提示。"""

    @staticmethod
    def _friendly_send_error(error: Exception) -> str:
        """生成友善的发送错误提示。"""
        error_str = str(error).lower()
        if isinstance(error, asyncio.TimeoutError) or "timeout" in error_str:
            return "图片上传超时，可能是图片太大或网络较慢，建议降低图片质量设置"
        if "cdn" in error_str or "upload" in error_str:
            return "图片上传到服务器失败，请稍后再试"
        if "network" in error_str or "connect" in error_str:
            return "网络连接异常，请检查网络后重试"
        return "发送失败，请稍后再试"
