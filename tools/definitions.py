"""四个模型工具的说明与独立参数定义；每次构造返回新字典。"""

from copy import deepcopy

from ..pixiv.constants import MAX_IMAGE_COUNT
from ..pixiv.query import MODES, RANKINGS, SEARCH_TARGETS
from ..pixiv.selection import SELECTION_PARAMETERS

MAX_TOOL_TAG_LENGTH = 48

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

IMAGE_PROPERTIES = {
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
        "minimum": 9.5367431640625e-07,
        "description": "本次文件大小上限，单位MiB，必须为正数；与管理员上限取较小值，不能放宽。省略沿用配置，50MiB硬上限不变。",
    },
}

ARTWORK_QUERY_OPTIONS = {
    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
    "include_stats": {
        "type": "boolean",
        "default": False,
        "description": "用户问点赞/热度或要求挑热门图时设true，按需补查本批作品的total_likes点赞、total_bookmarks收藏、total_view浏览。缺失为null，不等于0；补查有界且失败不影响候选。只比较本批，不是全站热门排序，不改变榜单顺序。续查只传cursor并继承此选项。",
    },
    "cursor": {
        "type": "string",
        "description": "续查只传cursor，不附其他参数；游标失效不自动重新查询。",
    },
}

USER_PARAMETERS = {
    "type": "object",
    "properties": {
        "word": {
            "type": "string",
            "maxLength": 48,
            "description": "作者名称关键词，与user_id二选一。",
        },
        "user_id": {"type": "string", "description": "明确Pixiv作者ID，与word二选一。"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5},
        "cursor": {"type": "string", "description": "续查只传cursor。"},
    },
    "additionalProperties": False,
}

IMAGE_DESCRIPTION = (
    "仅在用户明确要图片时向当前聊天取图。"
    "标签只取用户所需角色名或关键词，可换成确定的单一语言名称，不擅自加作品名、括号说明、画风或其他条件。"
    "模糊复数通常3张、可选2，明确数量优先。"
    "默认逐张直发，仅用户明确要求合并转发时选择forward，不因数量多而自行合并。"
    "可按用户要求筛选方向、比例、最小像素宽高与文件大小，不擅自添加筛选。"
    "数量按用户要求传入，插件限制数量时按返回原因解释，不猜测图库不足或标签失效，不主动建议换标签补取。"
    "找不够不放宽条件；"
    "只读尺寸，不识别图片；"
    "用户未明确要求时禁止主动调用视觉工具看图，“高质量”不构成识图授权。"
    "遵循插件安全与费用配置，不自动重试或补发。"
    "正常费用不向聊天报告。"
    "根据running与delivery判断执行状态，没有发送状态不得声称已发送；"
    "duplicate_call表示没有新增执行。"
    "支持按作品ID/链接及页码、榜单、作者、关联作品或明确Pixiv搜索直接取图；"
    "默认auto保留原行为。"
    "普通请求不先展示候选；"
    "用户要求热门或比较热度时先用get_px_artworks(include_stats=true)查候选，再按ID发送。"
    "日榜ranking_mode=day，周榜week，月榜month，保留上游榜单顺序。"
    "指定作品数量由targets决定；"
    "明确Pixiv列表模式保持上游顺序并按现有去重选取。"
)

CONTEXT_DESCRIPTION = (
    "只读当前用户余额、本会话取图限制及已配置图源与模式；"
    "配置状态未联网验证，不证明token有效。"
    "不建档、不写配置，无需每次发图前调用。"
    "只有用户问余额或限制时才报告数值。"
)

ARTWORKS_DESCRIPTION = (
    "仅查询公开作品详情或候选元数据，不发图、不扣费、不下载、不占去重。"
    "用户直接要图时优先get_px_image；"
    "用户要挑热门图时先查候选热度再选定作品发送；"
    "只有要挑选或查信息才列候选。"
    "支持相同取图模式；"
    "Pixiv可真实续查，Lolicon只有随机取样。"
    "字段不证明看过画面，不推断图库总量。"
    "用户未明确要求时禁止主动调用视觉工具看图，“热门/高质量”不是识图授权。"
    "日榜ranking_mode=day，周榜week，月榜month；"
    "看榜查询，要求发图才发送。"
)

USERS_DESCRIPTION = (
    "按名称找Pixiv作者或按明确ID查公开身份，不读取私人信息、账号关系或图片，不扣费。"
    "同名结果先交由用户确认，不擅自认定。"
    "得到明确ID后用get_px_image的author模式发图。"
    "需要插件配置Pixiv认证。"
)


def image_parameters():
    properties = deepcopy(IMAGE_PROPERTIES)
    properties.update(deepcopy(QUERY_PROPERTIES))
    return {"type": "object", "properties": properties, "additionalProperties": False}


def artwork_parameters():
    properties = deepcopy(QUERY_PROPERTIES)
    properties.update(
        {
            key: deepcopy(IMAGE_PROPERTIES[key])
            for key in {"tag"} | (SELECTION_PARAMETERS - {"max_size_mib"})
        }
    )
    for key in SELECTION_PARAMETERS - {"max_size_mib"}:
        properties[key]["description"] = (
            "仅按元数据筛选候选。"
            + properties[key]["description"].split("。", 1)[0]
            + "。不下载或检查实际文件。"
        )
    properties.update(deepcopy(ARTWORK_QUERY_OPTIONS))
    return {"type": "object", "properties": properties, "additionalProperties": False}


def user_parameters():
    return deepcopy(USER_PARAMETERS)
