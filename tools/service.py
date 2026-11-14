"""模型工具的请求编排、同事件幂等与生命周期。"""

from __future__ import annotations

import asyncio
import math
import time

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession

from ..pixiv.constants import MAX_IMAGE_COUNT
from ..pixiv.search_result import SearchResult
from ..pixiv.selection import ImageSelection, SELECTION_PARAMETERS
from ..pixiv.query import ArtworkQuery, QUERY_KEYS, MODES, QueryError
from ..pixiv.catalog import CandidateStream
from ..pixiv.discovery import DiscoveryService
from .definitions import MAX_TOOL_TAG_LENGTH
from .images import GetPxImageTool, GetPxContextTool
from .discovery import GetPxArtworksTool, GetPxUsersTool


def normalize_session(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("会话必须为完整 UMO")
    session = MessageSession.from_str(value.strip())
    if not session.platform_id.strip() or not session.session_id.strip():
        raise ValueError("会话必须包含平台实例和会话 ID")
    return str(session)


class ImageToolService:
    """请求内幂等与生命周期；不保存图片或另起后台取图。"""

    EVENT_KEY = "get_px_image_execution"

    def __init__(self, plugin):
        self.plugin = plugin
        self.active = True
        self._calls: set[asyncio.Task] = set()
        self.tools = []
        self.discovery = DiscoveryService(plugin)
        self._explicit_claims = set()

    def register(self):
        image = GetPxImageTool(self)
        self.tools = [
            image,
            GetPxContextTool(self),
            GetPxArtworksTool(self),
            GetPxUsersTool(self),
        ]
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
        self.discovery.clear()
        await self.discovery.stats_client.close()
        self._explicit_claims.clear()
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
        if not set(args).issubset(
            QUERY_KEYS | {"count", "send_mode"} | SELECTION_PARAMETERS
        ):
            raise ValueError("取图参数不受支持")
        query = ArtworkQuery.from_args(args)
        tag, count = args.get("tag", ""), args.get("count", 1)
        if query.mode == "artwork":
            if "count" in args and (
                type(args["count"]) is not int or args["count"] != len(query.targets)
            ):
                raise ValueError("指定作品的count必须等于合并重复目标后的数量")
            count = len(query.targets)
        send_mode = args.get("send_mode", "single")
        if (
            not isinstance(tag, str)
            or len(tag) > MAX_TOOL_TAG_LENGTH
            or any(ord(char) < 32 for char in tag)
        ):
            raise ValueError("标签必须为不含控制字符的短文本")
        if type(count) is not int or count < 1 or count > MAX_IMAGE_COUNT:
            raise ValueError("数量必须为范围内的正整数")
        if not isinstance(send_mode, str) or send_mode not in (
            "auto",
            "single",
            "forward",
        ):
            raise ValueError("发送方式必须为 auto、single 或 forward")
        selection = ImageSelection.from_args(args)
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
        holds = {(event.unified_msg_origin, target.key) for target in query.targets}
        acquired = False
        try:
            if holds & self._explicit_claims:
                raise QueryError(
                    "inflight", "指定作品正在执行，本次没有新增发送或消费。"
                )
            self._explicit_claims.update(holds)
            acquired = True
            # 留一半（最多30秒）给发送、结算和清理，实际发送仍遵循已有回执规则。
            deadline = time.monotonic() + timeout - min(30.0, timeout / 2)
            extra = {}
            if query.mode != "auto":
                extra["candidate_stream"] = CandidateStream(
                    self.plugin, event, query, None, selection, delivery=True
                )
            await self.plugin._execute_search(
                event,
                tag.strip(),
                str(count),
                tool=True,
                result=outcome,
                selection_deadline=deadline,
                send_mode=send_mode,
                selection=selection,
                **extra,
            )
        except asyncio.CancelledError:
            outcome.interrupted = True
            raise
        except QueryError as exc:
            outcome.message = str(exc)
        except Exception as exc:
            logger.warning(
                f"[GetPx] Tool 取图执行异常: error_type={type(exc).__name__}"
            )
            outcome.message = "取图执行未完整结束，请勿重复调用；发送或结算状态需核对。"
        finally:
            outcome.running = False
            self._calls.discard(task)
            if acquired:
                self._explicit_claims.difference_update(holds)
            if query.mode == "artwork":
                by_target = {
                    (item["id"], item["page"]): item for item in outcome.targets
                }
                outcome.targets = [
                    by_target.get(
                        (str(target.illust_id), target.page),
                        target.summary("not_submitted"),
                    )
                    for target in query.targets
                ]
                for target in outcome.targets:
                    if target["status"] == "candidate":
                        target["status"] = "not_submitted"
        return outcome.tool_summary()

    async def query(self, context, kind, args):
        task = asyncio.current_task()
        try:
            event = self._event(context)
            normalize_session(event.unified_msg_origin)
            timeout = context.tool_call_timeout
            if (
                type(timeout) not in (int, float)
                or not math.isfinite(timeout)
                or timeout <= 0
            ):
                raise ValueError("工具等待预算无效")
            self._calls.add(task)
            return await self.discovery.query(event, kind, args, timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self.discovery.error(exc)
        finally:
            self._calls.discard(task)

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
            coins = None
            if charging:
                wallet = self.plugin.checkin_store
                if wallet is None:
                    raise ValueError("余额服务暂不可用")
                profile = await wallet.find_profile(event.get_sender_id())
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
                "builtin_terms_enabled": policy.builtin_terms_enabled,
                "capabilities": {
                    "configured_sources": ["lolicon"]
                    + (
                        ["pixiv"]
                        if self.plugin._cfg_str("pixiv_refresh_token", "")
                        else []
                    ),
                    "available_modes": list(MODES)
                    if self.plugin._cfg_str("pixiv_refresh_token", "")
                    else ["auto"],
                    "pixiv_auth_verified": False,
                    "public_query_free": True,
                },
                "guidance": (
                    "仅为当前快照，不预留金币；发送工具会再次检查。不要主动报告金额或余额，只有用户明确询问时才说明。"
                    if charging
                    else "当前取图免费，不读取余额、不按金币限制数量；无需核对金币即可调用取图工具，仍受数量、频率与内容安全限制。"
                ),
            }
        finally:
            self._calls.discard(task)
