from __future__ import annotations

from dataclasses import dataclass, field

from .delivery import DeliveryStatus


@dataclass
class SearchResult:
    """仅保留执行事实；不向模型暴露图片路径、URL 或正常账单。"""

    requested_count: int
    max_count: int = 0
    count_limits: list[str] = field(default_factory=list)
    count: int = 0
    downloaded_count: int = 0
    delivery: dict[str, int] = field(default_factory=dict)
    settlement: str = "not_required"
    message: str = ""
    notice: str = ""
    interrupted: bool = False
    running: bool = True

    def record_delivery(self, status: DeliveryStatus, count: int) -> None:
        self.delivery[status.value] = self.delivery.get(status.value, 0) + count

    def tool_summary(self) -> dict:
        uncertain = any(
            self.delivery.get(state, 0)
            for state in ("accepted", "timed_out", "unknown")
        )
        return {
            "running": self.running,
            "requested_count": self.requested_count,
            "effective_count": self.count,
            "max_count": self.max_count,
            "count_limits": list(self.count_limits),
            "downloaded_count": self.downloaded_count,
            "delivery": dict(self.delivery),
            "settlement": self.settlement,
            "message": self.message,
            "notice": self.notice,
            "interrupted": self.interrupted,
            "retry_allowed": False,
            "guidance": (
                "图片由插件直接发送，不要再次发送或自动补取。"
                "running 为 true 时仍在执行；没有 delivery 状态时不得声称已发送。"
                "数量缩减按 count_limits 和 notice 解释，不猜测图库不足或标签失效，不主动建议换标签补取。"
                "发送调用返回不等于用户实际收到；不主动报告金额或余额。"
                + ("送达尚未确认，请勿宣称已收到。" if uncertain else "")
            ),
        }
