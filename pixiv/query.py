"""模型公开查询的输入边界；不接受任意网络地址或账号凭据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from urllib.parse import urlsplit

MODES = ("auto", "search", "recommended", "ranking", "author", "related", "artwork")
RANKINGS = (
    "day",
    "week",
    "month",
    "week_rookie",
    "week_original",
    "day_male",
    "day_female",
)
SEARCH_TARGETS = ("partial_match_for_tags", "exact_match_for_tags", "title_and_caption")
QUERY_KEYS = frozenset(
    {
        "mode",
        "tag",
        "search_target",
        "sort",
        "start_date",
        "end_date",
        "ranking_mode",
        "date",
        "user_id",
        "illust_id",
        "targets",
    }
)


class QueryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def positive_id(value: object, *, artwork: bool = False) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str):
        if artwork and value.startswith("https://"):
            parsed = urlsplit(value)
            match = re.fullmatch(
                r"/(?:[a-z]{2}/)?artworks/([1-9][0-9]*)/?", parsed.path
            )
            if (
                parsed.netloc in ("www.pixiv.net", "pixiv.net")
                and match
                and not parsed.query
                and not parsed.fragment
            ):
                return int(match[1])
        if re.fullmatch(r"[1-9][0-9]{0,19}", value):
            return int(value)
    raise QueryError("invalid_request", "需要正整数 ID 或标准 Pixiv 作品链接。")


@dataclass(frozen=True)
class ArtworkTarget:
    illust_id: int
    page: int = 1

    @property
    def key(self) -> str:
        return (
            str(self.illust_id)
            if self.page == 1
            else f"{self.illust_id}:{self.page - 1}"
        )

    def summary(self, status: str) -> dict:
        return {"id": str(self.illust_id), "page": self.page, "status": status}


@dataclass(frozen=True)
class ArtworkQuery:
    mode: str = "auto"
    tag: str = ""
    search_target: str = "partial_match_for_tags"
    sort: str = "date_desc"
    start_date: str | None = None
    end_date: str | None = None
    ranking_mode: str = "day"
    date: str | None = None
    user_id: int | None = None
    illust_id: int | None = None
    targets: tuple[ArtworkTarget, ...] = ()

    @classmethod
    def from_args(cls, args: dict) -> ArtworkQuery:
        values = {key: value for key, value in args.items() if key in QUERY_KEYS}
        mode = values.get("mode", "auto")
        if not isinstance(mode, str) or mode not in MODES:
            raise QueryError("invalid_request", "取图模式无效。")
        allowed = {
            "auto": {"tag"},
            "search": {"tag", "search_target", "sort", "start_date", "end_date"},
            "ranking": {"ranking_mode", "date"},
            "author": {"user_id"},
            "related": {"illust_id"},
            "artwork": {"targets"},
            "recommended": set(),
        }[mode]
        if set(values) - allowed - {"mode"}:
            raise QueryError("invalid_request", "参数与取图模式不匹配。")
        tag = values.get("tag", "")
        if not isinstance(tag, str) or len(tag) > 48 or any(ord(c) < 32 for c in tag):
            raise QueryError(
                "invalid_request", "标签必须为不含控制字符的短文本，最多48字符。"
            )
        values["tag"] = tag.strip()
        if mode == "search" and not tag.strip():
            raise QueryError("invalid_request", "搜索模式需要非空关键词。")
        if values.get("search_target", SEARCH_TARGETS[0]) not in SEARCH_TARGETS:
            raise QueryError("invalid_request", "搜索匹配方式无效。")
        if values.get("sort", "date_desc") not in ("date_desc", "date_asc"):
            raise QueryError("invalid_request", "只支持最新或最早排序。")
        if values.get("ranking_mode", "day") not in RANKINGS:
            raise QueryError("invalid_request", "榜单类型无效。")
        for key in ("start_date", "end_date", "date"):
            if key in values:
                value = values[key]
                if not isinstance(value, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}", value
                ):
                    raise QueryError("invalid_request", "日期需为 YYYY-MM-DD。")
                try:
                    date.fromisoformat(value)
                except ValueError as exc:
                    raise QueryError("invalid_request", "日期不存在。") from exc
        if values.get("start_date", "") > values.get("end_date", "9999-12-31"):
            raise QueryError("invalid_request", "开始日期不能晚于结束日期。")
        for key, required_mode in (("user_id", "author"), ("illust_id", "related")):
            if mode == required_mode:
                values[key] = positive_id(values.get(key), artwork=key == "illust_id")
        if mode == "artwork":
            raw = values.get("targets")
            if not isinstance(raw, list) or not 1 <= len(raw) <= 20:
                raise QueryError("invalid_request", "指定作品列表需要1至20项目标。")
            targets = []
            for item in raw:
                if not isinstance(item, dict) or set(item) - {"artwork", "page"}:
                    raise QueryError(
                        "invalid_request", "作品目标只接受 artwork 和 page。"
                    )
                page = item.get("page", 1)
                if type(page) is not int or page < 1:
                    raise QueryError("invalid_request", "作品页码从1开始。")
                target = ArtworkTarget(
                    positive_id(item.get("artwork"), artwork=True), page
                )
                if target not in targets:
                    targets.append(target)
            values["targets"] = tuple(targets)
        return cls(**values)
