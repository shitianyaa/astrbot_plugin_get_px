from __future__ import annotations

from dataclasses import dataclass, field

from .delivery import DeliveryStatus


@dataclass
class SearchResult:
    """仅保留执行事实；不向模型暴露图片路径、URL 或正常账单。"""

    requested_count: int
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
            "downloaded_count": self.downloaded_count,
            "delivery": dict(self.delivery),
            "settlement": self.settlement,
            "message": self.message,
            "notice": self.notice,
            "interrupted": self.interrupted,
            "retry_allowed": False,
            "guidance": (
                "图片由插件直接发送，不要再次发送或自动补取。"
                "发送调用返回不等于用户实际收到；不主动报告金额或余额。"
                + ("送达尚未确认，请勿宣称已收到。" if uncertain else "")
            ),
        }
