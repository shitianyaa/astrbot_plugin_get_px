"""模型请求的同事件任务复用、串行发送及共享预算。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import time

from .results import ToolResult, image_summary


@dataclass
class EventRequests:
    limit: int
    deadline: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    results: dict = field(default_factory=dict)
    used: int = 0
    admitted: bool = False
    query_deadline: float | None = None

    @classmethod
    def for_event(cls, event, limit, timeout):
        key = "get_px_tool_requests"
        state = event.get_extra(key)
        if state is None:
            state = cls(limit, time.monotonic() + timeout)
            event.set_extra(key, state)
        return state

    def duplicate(self, key):
        result = self.results.get(key)
        if result is None or (not result.running and result.retry_allowed):
            return None
        summary = image_summary(result)
        summary.update(duplicate_call=True, notice="", retry_allowed=False)
        summary["guidance"] += (
            "这是同一取图任务的已有结果，没有新增发送；其他不同任务可继续。"
        )
        return summary

    async def send(self, key, count, event, admit, execute):
        existing = self.duplicate(key)
        if existing is not None:
            return existing
        async with self.lock:
            existing = self.duplicate(key)
            if existing is not None:
                return existing
            outcome = ToolResult(count)
            outcome.max_count = self.limit
            self.results[key] = outcome
            event.set_extra("get_px_image_execution", outcome)
            previous_used = self.used
            allowed = min(count, max(0, self.limit - self.used))
            try:
                if time.monotonic() >= self.deadline:
                    outcome.message = "本条消息的取图等待预算已用尽，本任务未开始发送。"
                elif not allowed:
                    outcome.message = (
                        "本条消息的取图总张数已达配置上限，本任务未开始发送。"
                    )
                    outcome.count_limits.append("event_limit")
                else:
                    wait = 0 if self.admitted else admit()
                    if wait:
                        outcome.message = (
                            f"请求触发频率限制，还需等待 {wait} 秒，本任务未开始取图。"
                        )
                        outcome.retry_allowed = True
                    else:
                        self.admitted = True
                        await execute(outcome, allowed, self.deadline)
            finally:
                outcome.running = False
                outcome.requested_count = count
                submitted = sum(
                    n for status, n in outcome.delivery.items() if status != "failed"
                )
                self.used += (
                    allowed
                    if outcome.submission_uncertain and not outcome.delivery
                    else submitted
                )
                if time.monotonic() >= self.deadline:
                    outcome.retry_allowed = False
                if allowed and allowed < count:
                    if previous_used:
                        outcome.count_limits.append("event_limit")
                        outcome.notice += (
                            f"本条消息剩余名额为 {allowed} 张，其余未执行。"
                        )
                    else:
                        outcome.count_limits.insert(0, "max_count")
                        outcome.notice = (
                            f"本次请求 {count} 张，插件单次上限为 {self.limit} 张。"
                            + outcome.notice
                        )
            return image_summary(outcome)
