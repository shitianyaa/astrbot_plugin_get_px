"""向模型投影取图事实与下一步提示；不承载发送或结算。"""

from dataclasses import dataclass

from ..pixiv.search_result import SearchResult


@dataclass
class ToolResult(SearchResult):
    retry_allowed: bool = False
    submission_uncertain: bool = False


def image_summary(result) -> dict:
    uncertain = any(
        result.delivery.get(state, 0) for state in ("accepted", "timed_out", "unknown")
    )
    summary = {
        "running": result.running,
        "requested_count": result.requested_count,
        "effective_count": result.count,
        "max_count": result.max_count,
        "count_limits": list(result.count_limits),
        "downloaded_count": result.downloaded_count,
        "delivery": dict(result.delivery),
        "settlement": result.settlement,
        "message": result.message,
        "notice": result.notice,
        "interrupted": result.interrupted,
        "retry_allowed": result.retry_allowed,
        "guidance": (
            "本结果只属于当前取图任务，其他不同任务可继续。"
            "running 为 true 时仍在执行；没有 delivery 状态时不得声称已发送。"
            "数量缩减按 count_limits 和 notice 的实际原因解释。"
            "发送调用返回不等于用户实际收到；不主动报告金额或余额。"
            + ("送达尚未确认，请勿宣称已收到。" if uncertain else "")
        ),
    }
    if result.targets:
        summary["targets"] = [dict(target) for target in result.targets]
    if result.retry_allowed:
        summary["guidance"] += (
            "本任务未提交图片，可在原需求内调整条件或重试；遵守本消息总预算。"
        )
    else:
        summary["guidance"] += "不要重试已提交或送达待确认的任务。"
    return summary
