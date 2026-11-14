"""发图与当前用户状态工具；参数定义和执行编排分别独立。"""

from __future__ import annotations

import json

from astrbot.api import FunctionTool, logger

from .definitions import IMAGE_DESCRIPTION, CONTEXT_DESCRIPTION, image_parameters


class GetPxImageTool(FunctionTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_image",
            description=IMAGE_DESCRIPTION,
            parameters=image_parameters(),
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
            description=CONTEXT_DESCRIPTION,
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
