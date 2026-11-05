from __future__ import annotations

import asyncio
import json
import re
import time

from astrbot.api import logger


def _safe_error_text(value: object) -> str:
    """保留诊断文本，移除地址、标识和凭据；限制异常回包撑大日志。"""
    if not isinstance(value, str):
        return ""
    text = value
    text = re.sub(r"https?://\S+|\[CQ:[^\]]*\]", "[redacted]", text)
    text = re.sub(r"(?i)(?:[a-z]:[\\/]|/)[^\s\"']+", "[path]", text)
    text = re.sub(
        r'''(?i)((?:token|secret|password|authorization|api[_-]?key)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
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


async def send_once(event, content: list, *, context: str) -> bool:
    """发送接口无幂等保证；异常时保留未知状态，不自动重发图片。"""
    started = time.monotonic()
    try:
        message = event.chain_result(content)
    except Exception as exc:
        log_delivery_error(
            exc, context=context, phase="build", delivery_status="failed"
        )
        return False
    try:
        await event.send(message)
    except Exception as exc:
        log_delivery_error(
            exc,
            context=context,
            phase="send",
            delivery_status="unknown",
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        try:
            await event.send(
                event.plain_result(
                    "⚠️ 图片送达状态未知，已停止自动重发；请先查看聊天记录，本次未确认部分不扣费。"
                )
            )
        except Exception as notice_error:
            log_delivery_error(
                notice_error, context=context, phase="notice", delivery_status="unknown"
            )
        return False
    logger.info(
        f"[GetPx] 作品发送已确认: {context} delivery_status=confirmed duration_ms={int((time.monotonic() - started) * 1000)}"
    )
    return True


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
