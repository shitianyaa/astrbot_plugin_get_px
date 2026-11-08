from __future__ import annotations

import asyncio
import json
import math
import time

from astrbot.api import FunctionTool, logger

from ..group_safety import normalize_session
from ..pixiv.constants import MAX_IMAGE_COUNT
from ..pixiv.search_result import SearchResult


MAX_TOOL_TAG_LENGTH = 48


class ImageToolService:
    """请求内幂等与生命周期；不保存图片或另起后台取图。"""

    EVENT_KEY = "get_px_image_execution"

    def __init__(self, plugin):
        self.plugin = plugin
        self.active = True
        self._calls: set[asyncio.Task] = set()
        self.tools = []

    def register(self):
        self.tools = [GetPxImageTool(self), GetPxContextTool(self)]
        self.plugin.context.add_llm_tools(*self.tools)
        # 旧框架 add_llm_tools 的归属解析不完整，显式指向已注册入口模块。
        for tool in self.tools:
            tool.handler_module_path = type(self.plugin).__module__

    async def close(self):
        self.active = False
        for tool in self.tools:
            tool.active = False
        tasks = tuple(self._calls)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        manager = self.plugin.context.get_llm_tool_manager()
        manager.func_list[:] = [
            tool
            for tool in manager.func_list
            if all(tool is not owned for owned in self.tools)
        ]

    def _event(self, context):
        event = context.context.event
        if not self.active or event is None:
            raise ValueError("取图工具暂不可用")
        if (
            not isinstance(event.get_sender_id(), str)
            or not event.get_sender_id().strip()
        ):
            raise ValueError("无法确认当前用户")
        return event

    async def image(self, context, args):
        if not set(args).issubset({"tag", "count", "send_mode"}):
            raise ValueError("取图参数只支持标签、数量和发送方式")
        tag, count = args.get("tag", ""), args.get("count", 1)
        send_mode = args.get("send_mode", "auto")
        if (
            not isinstance(tag, str)
            or len(tag) > MAX_TOOL_TAG_LENGTH
            or any(ord(char) < 32 for char in tag)
        ):
            raise ValueError("标签必须为不含控制字符的短文本")
        if type(count) is not int or count < 1 or count > MAX_IMAGE_COUNT:
            raise ValueError("数量必须为范围内的正整数")
        if not isinstance(send_mode, str) or send_mode not in ("auto", "single", "forward"):
            raise ValueError("发送方式必须为 auto、single 或 forward")
        event = self._event(context)
        # 先校验会话，不能给缺失 UMO 的请求默认全局放行。
        normalize_session(event.unified_msg_origin)
        existing = event.get_extra(self.EVENT_KEY)
        if existing is not None:
            summary = existing.tool_summary()
            summary["duplicate_call"] = True
            summary["notice"] = ""
            summary["guidance"] += "此请求已执行或正在执行；新增取图请另发一条新消息。"
            return summary
        timeout = context.tool_call_timeout
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("工具等待预算无效")
        outcome = SearchResult(count)
        event.set_extra(self.EVENT_KEY, outcome)
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            # 留一半（最多30秒）给发送、结算和清理，实际发送仍遵循已有回执规则。
            deadline = time.monotonic() + timeout - min(30.0, timeout / 2)
            await self.plugin._execute_search(
                event,
                tag.strip(),
                str(count),
                tool=True,
                result=outcome,
                selection_deadline=deadline,
                send_mode=send_mode,
            )
        except asyncio.CancelledError:
            outcome.interrupted = True
            raise
        except Exception as exc:
            logger.warning(
                f"[GetPx] Tool 取图执行异常: error_type={type(exc).__name__}"
            )
            outcome.message = "取图执行未完整结束，请勿重复调用；发送或结算状态需核对。"
        finally:
            outcome.running = False
            self._calls.discard(task)
        return outcome.tool_summary()

    async def get_context(self, context, args):
        if args:
            raise ValueError("状态工具不接受参数")
        event = self._event(context)
        normalize_session(event.unified_msg_origin)
        task = asyncio.current_task()
        self._calls.add(task)
        try:
            policy = await self.plugin._content_safety_policy(event)
            cost = self.plugin._p_unit_cost()
            charging = cost > 0 and self.plugin._cfg_bool("checkin_enabled", True)
            wallet = self.plugin.checkin_store
            if charging and wallet is None:
                raise ValueError("余额服务暂不可用")
            profile = (
                await wallet.find_profile(event.get_sender_id())
                if wallet is not None
                else None
            )
            coins = profile.coins if profile is not None else 0
            return {
                "balance": coins,
                "charging": charging,
                "unit_cost": cost if charging else 0,
                "affordable_count": coins // cost if charging else None,
                "max_count": self.plugin._cfg_int("max_count", 5, 1, MAX_IMAGE_COUNT),
                "preferred_source": self.plugin._cfg_str(
                    "preferred_image_source", "Lolicon"
                ),
                "quality": self.plugin._cfg_str("image_quality", "original"),
                "size_limit_mib": self.plugin._cfg_float(
                    "auto_downgrade_original_mb", 10.0, 0.0, 25.0
                ),
                "forward_threshold": self.plugin._forward_threshold(),
                "allow_r18": not policy.general_only_enabled,
                "keyword_match_mode": policy.match_mode,
                "guidance": "仅为当前快照，不预留金币；发送工具会再次检查。不要主动报告金额或余额，只有用户明确询问时才说明。",
            }
        finally:
            self._calls.discard(task)


class GetPxImageTool(FunctionTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_image",
            description="仅在用户明确要图片时向当前聊天取图。标签只取用户所需角色名或关键词，可换成确定的单一语言名称，不擅自加作品名、括号说明、画风或其他条件。模糊复数通常3张、可选2，明确数量优先。可自行选择直发或合并，用户指定优先，少量优先直发。遵循插件安全与费用配置，不自动重试或补发。正常费用不向聊天报告。",
            parameters={
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "用户所需的短标签，最多48字符；角色可用原名或一个确定的译名，不拼多语言别名，不擅自扩写。空串为随机。",
                        "maxLength": MAX_TOOL_TAG_LENGTH,
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_IMAGE_COUNT,
                        "default": 1,
                    },
                    "send_mode": {
                        "type": "string",
                        "enum": ["auto", "single", "forward"],
                        "default": "auto",
                        "description": "single逐张直发；forward合并转发（仅OneBot，其他平台逐张直发）；auto或省略沿用配置阈值。用户指定优先，否则自行选择，少量图片优先single，多图需收纳可forward。只影响本次调用；提交后不得换方式补发。",
                    },
                },
                "additionalProperties": False,
            },
        )
        self.service = service

    async def call(self, context, **kwargs):
        if not self.active:
            return json.dumps(
                {"message": "取图工具已停用，本次未执行。", "retry_allowed": False},
                ensure_ascii=False,
            )
        try:
            result = await self.service.image(context, kwargs)
        except Exception as exc:
            logger.warning(f"[GetPx] Tool 请求已拒绝: error_type={type(exc).__name__}")
            result = {
                "message": "参数、会话或服务状态不满足取图要求，本次未开始新的取图。",
                "retry_allowed": False,
            }
        return json.dumps(result, ensure_ascii=False)


class GetPxContextTool(FunctionTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_context",
            description="只读当前用户余额与本会话取图限制；不建档、不写配置。无需每次发图前调用。只有用户问余额或限制时才报告数值。",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        )
        self.service = service

    async def call(self, context, **kwargs):
        if not self.active:
            return json.dumps({"message": "状态工具已停用。"}, ensure_ascii=False)
        try:
            result = await self.service.get_context(context, kwargs)
        except Exception as exc:
            logger.warning(
                f"[GetPx] Tool 状态读取失败: error_type={type(exc).__name__}"
            )
            result = {"message": "当前状态无法读取，不应推断免费或余额充足。"}
        return json.dumps(result, ensure_ascii=False)
