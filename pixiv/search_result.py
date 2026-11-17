from __future__ import annotations

from dataclasses import dataclass, field

from .delivery import DeliveryStatus


@dataclass
class SearchResult:
    """取图执行事实，供命令和工具共用；模型反馈由 tools/ 投影。"""

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
    submission_started: bool = False
    running: bool = True
    targets: list[dict] = field(default_factory=list)

    def record_delivery(self, status: DeliveryStatus, count: int) -> None:
        self.delivery[status.value] = self.delivery.get(status.value, 0) + count
