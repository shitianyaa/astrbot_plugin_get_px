"""公开作品与作者工具；参数说明集中复用取图模式。"""

from __future__ import annotations

import json
from copy import deepcopy

from astrbot.api import FunctionTool

from ..pixiv.query import MODES, RANKINGS, SEARCH_TARGETS

QUERY_PROPERTIES = {
    "mode": {
        "type": "string",
        "enum": list(MODES),
        "default": "auto",
        "description": "auto保留配置的两源取图；其余模式明确使用Pixiv，不跨源替代。search搜索，recommended插件账号推荐，ranking榜单，author作者作品，related关联作品，artwork指定作品。",
    },
    "search_target": {
        "type": "string",
        "enum": list(SEARCH_TARGETS),
        "description": "仅search：标签部分匹配、精确匹配或标题描述。",
    },
    "sort": {
        "type": "string",
        "enum": ["date_desc", "date_asc"],
        "description": "仅search，最新或最早优先，不支持热门排序。",
    },
    "start_date": {"type": "string", "description": "仅search，开始日期YYYY-MM-DD。"},
    "end_date": {"type": "string", "description": "仅search，结束日期YYYY-MM-DD。"},
    "ranking_mode": {
        "type": "string",
        "enum": list(RANKINGS),
        "description": "仅ranking：日、周、月、新人、原创、男性向、女性向榜；默认day。过滤后不保证是原榜前几名。",
    },
    "date": {
        "type": "string",
        "description": "仅ranking，指定日期YYYY-MM-DD，省略采用上游最新榜单。",
    },
    "user_id": {
        "type": "string",
        "description": "仅author，明确的Pixiv作者ID。名称先用get_px_users查询，同名不擅自选择。",
    },
    "illust_id": {
        "type": "string",
        "description": "仅related，种子作品ID或标准Pixiv作品链接；关联不代表视觉相似。",
    },
    "targets": {
        "type": "array",
        "minItems": 1,
        "maxItems": 20,
        "description": "仅artwork：明确指定的作品和单页；重复目标合并，数量由目标数决定，不与count矛盾；不取整部，不换其他作品补齐。",
        "items": {
            "type": "object",
            "properties": {
                "artwork": {
                    "type": "string",
                    "description": "作品ID或https://www.pixiv.net/artworks/ID标准链接。",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "页码从1开始，默认首张，越界不会回退首张。",
                },
            },
            "required": ["artwork"],
            "additionalProperties": False,
        },
    },
}


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
    def __init__(self, service, image_properties):
        from ..pixiv.selection import SELECTION_PARAMETERS

        properties = deepcopy(QUERY_PROPERTIES)
        properties.update(
            {
                key: deepcopy(image_properties[key])
                for key in {"tag"} | (SELECTION_PARAMETERS - {"max_size_mib"})
            }
        )
        for key in SELECTION_PARAMETERS - {"max_size_mib"}:
            properties[key]["description"] = (
                "仅按元数据筛选候选。"
                + properties[key]["description"].split("。", 1)[0]
                + "。不下载或检查实际文件。"
            )
        properties.update(
            {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
                "cursor": {
                    "type": "string",
                    "description": "续查只传cursor，不附其他参数；游标失效不自动重新查询。",
                },
            }
        )
        super().__init__(
            name="get_px_artworks",
            description="仅查询公开作品详情或候选元数据，不发图、不扣费、不下载、不占去重。用户直接要图时优先get_px_image；只有要挑选或查信息才列候选。支持相同取图模式；Pixiv可真实续查，Lolicon只有随机取样。字段不证明看过画面，不推断图库总量。",
            parameters={
                "type": "object",
                "properties": properties,
                "additionalProperties": False,
            },
        )
        self.service, self.kind = service, "artworks"


class GetPxUsersTool(PublicQueryTool):
    def __init__(self, service):
        super().__init__(
            name="get_px_users",
            description="按名称找Pixiv作者或按明确ID查公开身份，不读取私人信息、账号关系或图片，不扣费。同名结果先交由用户确认，不擅自认定。得到明确ID后用get_px_image的author模式发图。需要插件配置Pixiv认证。",
            parameters={
                "type": "object",
                "properties": {
                    "word": {
                        "type": "string",
                        "maxLength": 48,
                        "description": "作者名称关键词，与user_id二选一。",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "明确Pixiv作者ID，与word二选一。",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "cursor": {"type": "string", "description": "续查只传cursor。"},
                },
                "additionalProperties": False,
            },
        )
        self.service, self.kind = service, "users"
