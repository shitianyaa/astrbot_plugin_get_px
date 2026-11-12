from __future__ import annotations

import asyncio
import json
import math
import time

from astrbot.api import FunctionTool, logger

from astrbot.core.platform.message_session import MessageSession
from ..pixiv.constants import MAX_IMAGE_COUNT
from ..pixiv.search_result import SearchResult
from ..pixiv.selection import ImageSelection, SELECTION_PARAMETERS
from ..pixiv.query import ArtworkQuery, QUERY_KEYS, MODES, QueryError
from ..pixiv.catalog import CandidateStream
from ..pixiv.discovery import DiscoveryService
from .discovery import QUERY_PROPERTIES, GetPxArtworksTool, GetPxUsersTool


MAX_TOOL_TAG_LENGTH = 48


def normalize_session(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(char) < 32 for char in value):
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
        self.tools = [image, GetPxContextTool(self), GetPxArtworksTool(self, image.parameters["properties"]), GetPxUsersTool(self)]
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
        if not set(args).issubset(QUERY_KEYS | {"count", "send_mode"} | SELECTION_PARAMETERS):
            raise ValueError("取图参数不受支持")
        query = ArtworkQuery.from_args(args)
        tag, count = args.get("tag", ""), args.get("count", 1)
        if query.mode == "artwork":
            if "count" in args and (type(args["count"]) is not int or args["count"] != len(query.targets)):
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
        if not isinstance(send_mode, str) or send_mode not in ("auto", "single", "forward"):
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
                raise QueryError("inflight", "指定作品正在执行，本次没有新增发送或消费。")
            self._explicit_claims.update(holds)
            acquired = True
            # 留一半（最多30秒）给发送、结算和清理，实际发送仍遵循已有回执规则。
            deadline = time.monotonic() + timeout - min(30.0, timeout / 2)
            extra = {}
            if query.mode != "auto":
                extra["candidate_stream"] = CandidateStream(self.plugin, event, query, None, selection, delivery=True)
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
                by_target = {(item["id"], item["page"]): item for item in outcome.targets}
                outcome.targets = [by_target.get((str(target.illust_id), target.page), target.summary("not_submitted")) for target in query.targets]
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
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
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
                    "configured_sources": ["lolicon"] + (["pixiv"] if self.plugin._cfg_str("pixiv_refresh_token", "") else []),
                    "available_modes": list(MODES) if self.plugin._cfg_str("pixiv_refresh_token", "") else ["auto"],
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


class GetPxImageTool(FunctionTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_image",
            description="仅在用户明确要图片时向当前聊天取图。标签只取用户所需角色名或关键词，可换成确定的单一语言名称，不擅自加作品名、括号说明、画风或其他条件。模糊复数通常3张、可选2，明确数量优先。默认逐张直发，仅用户明确要求合并转发时选择forward，不因数量多而自行合并。可按用户要求筛选方向、比例、最小像素宽高与文件大小，不擅自添加筛选。数量按用户要求传入，插件限制数量时按返回原因解释，不猜测图库不足或标签失效，不主动建议换标签补取。找不够不放宽条件；只读尺寸，不识别图片。遵循插件安全与费用配置，不自动重试或补发。正常费用不向聊天报告。根据running与delivery判断执行状态，没有发送状态不得声称已发送；duplicate_call表示没有新增执行。",
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
                        "description": "普通取图省略为1；artwork省略时按合并重复目标后的数量，若传count须与目标数相等。按用户明确数量传入，插件按管理员上限和余额限制执行并说明原因，不拆分绕过限制。",
                    },
                    "send_mode": {
                        "type": "string",
                        "enum": ["auto", "single", "forward"],
                        "default": "single",
                        "description": "single或省略逐张直发；仅用户明确要求合并转发时选择forward（仅OneBot，其他平台逐张直发），不因数量多而自行合并。auto沿用配置阈值，仅用户明确要求按配置自动选择时使用。只影响本次调用；提交后不得换方式补发。",
                    },
                    "orientation": {
                        "type": "string",
                        "enum": ["any", "landscape", "portrait", "square"],
                        "default": "any",
                        "description": "any不限方向；landscape宽大于高；portrait宽小于高；square宽高相等。只按用户要求指定，与其他筛选同时生效。",
                    },
                    "aspect_ratio": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "目标宽/高，例如16:9传1.7777778、3:4传0.75。省略容差时取0.1与目标比例一半中的较小值；省略不限制比例。",
                    },
                    "ratio_tolerance": {
                        "type": "number",
                        "minimum": 0,
                        "description": "宽高比的绝对容差，需同时指定aspect_ratio且小于目标比例；省略为min(0.1,目标比例/2)，0为精确比例，不是百分比。",
                    },
                    "min_width": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "实际发送文件最小像素宽度；0不限制。元数据粗筛后再读文件尺寸，不识别画面。",
                    },
                    "min_height": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "实际发送文件最小像素高度；0不限制。所选画质达不到就跳过，不换画质。",
                    },
                    "max_size_mib": {
                        "type": "number",
                        "minimum": 1 / (1024 * 1024),
                        "description": "本次文件大小上限，单位MiB，必须为正数；与管理员上限取较小值，不能放宽。省略沿用配置，50MiB硬上限不变。",
                    },
                },
                "additionalProperties": False,
            },
        )
        self.service = service
        self.parameters["properties"].update(QUERY_PROPERTIES)
        self.description += "支持按作品ID/链接及页码、榜单、作者、关联作品或明确Pixiv搜索直接取图；默认auto保留原行为。普通请求不先展示候选。指定作品数量由targets决定；明确Pixiv列表模式保持上游顺序并按现有去重选取。"

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
            description="只读当前用户余额、本会话取图限制及已配置图源与模式；配置状态未联网验证，不证明token有效。不建档、不写配置，无需每次发图前调用。只有用户问余额或限制时才报告数值。",
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
