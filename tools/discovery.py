"""公开作品与作者查询的原生工具入口。"""

from __future__ import annotations

import json

from astrbot.api import FunctionTool

from .definitions import (
    ARTWORKS_DESCRIPTION,
    USERS_DESCRIPTION,
    artwork_parameters,
    user_parameters,
)


class PublicQueryTool(FunctionTool):
    async def call(self, context, **kwargs):
        if not self.active:
            result = {
                "status": "unavailable",
                "records": [],
                "message": "公开查询工具已停用。",
                "retry_allowed": False,
            }
        else:
            result = await self.service.query(context, self.kind, kwargs)
        return json.dumps(result, ensure_ascii=False)


class GetPxArtworksTool(PublicQueryTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_artworks",
            description=ARTWORKS_DESCRIPTION,
            parameters=artwork_parameters(),
        )
        self.service, self.kind = service, "artworks"


class GetPxUsersTool(PublicQueryTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_users",
            description=USERS_DESCRIPTION,
            parameters=user_parameters(),
        )
        self.service, self.kind = service, "users"
