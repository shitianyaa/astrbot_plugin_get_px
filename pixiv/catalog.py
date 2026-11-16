"""有界公开作品流，供只读查询和共享发图流程使用。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib

from astrbot.api import logger

from .client import PixivPage
from .query import ArtworkQuery, ArtworkTarget, QueryError
from .selection import ImageSelection


def artwork_record(item: dict) -> dict:
    identity = str(item.get("pid") or item.get("id") or "").partition(":")[0]
    result = {
        "id": identity,
        "title": str(item.get("title") or ""),
        "author": user_record(item.get("user") or {}),
        "tags": [
            str(t.get("name") or "")
            for t in item.get("tags") or []
            if isinstance(t, dict)
        ],
        "width": item.get("width", 0),
        "height": item.get("height", 0),
        "type": item.get("type", "illust"),
        "url": f"https://www.pixiv.net/artworks/{identity}",
    }
    if "_page" in item:
        result["page"] = item["_page"]
    elif item.get("_source") == "lolicon":
        result["page"] = item["page"] + 1
    if type(item.get("page_count")) is int and item["page_count"] > 0:
        result["page_count"] = item["page_count"]
    for key in ("total_view", "total_bookmarks", "create_date"):
        if key in item:
            result[key] = item[key]
    return result


def user_record(user: dict) -> dict:
    identity = str(user.get("id") or "")
    return {
        "id": identity,
        "name": str(user.get("name") or ""),
        "account": str(user.get("account") or ""),
        "url": f"https://www.pixiv.net/users/{identity}",
    }


@dataclass
class CandidateStream:
    plugin: object
    event: object
    query: ArtworkQuery
    policy: object
    selection: ImageSelection = field(default_factory=ImageSelection)
    delivery: bool = False
    continuation: dict | None = None
    started: bool = False
    exhausted: bool = False
    sampled: bool = False
    raw_count: int = 0
    filtered_count: int = 0
    target_results: list[dict] = field(default_factory=list)

    @property
    def source_key(self):
        if self.query.mode == "artwork":
            return "pixiv:artwork"
        digest = hashlib.sha256(repr(self.query).encode()).hexdigest()[:20]
        return f"pixiv:{self.query.mode}:{digest}"

    def client(self):
        if getattr(self.plugin, "client", None) is None:
            self.plugin._init_client()
        client = getattr(self.plugin, "client", None)
        if client is None:
            raise QueryError(
                "authentication_required", "此模式需要配置可用的 Pixiv 账号认证。"
            )
        return client

    async def filter(self, items: list[dict], *, dimensions: bool = True) -> list[dict]:
        candidates = items
        if self.plugin._cfg_bool("filter_manga", True):
            candidates = self.plugin._filter_manga(candidates)
        candidates = await self.plugin._filter_blacklisted_illusts(
            candidates, self.policy
        )
        return [
            item
            for item in candidates
            if item.get("type") != "ugoira"
            and item.get("visible") is not False
            and item.get("restrict", 0) in (0, "0", None)
            and (
                not dimensions
                or (self.delivery and item.get("_page", 1) > 1)
                or self.selection.matches_dimensions(
                    item.get("width"), item.get("height")
                )
            )
        ]

    async def _targets(self) -> PixivPage:
        items, details = [], {}
        for target in self.query.targets:
            if target.illust_id not in details:
                details[target.illust_id] = await self.client().public_detail(
                    target.illust_id
                )
            original = details[target.illust_id]
            status = "not_found"
            if original:
                if not await self.filter([original], dimensions=False):
                    status = "filtered"
                    self.filtered_count += 1
                else:
                    pages = original.get("meta_pages") or []
                    if target.page > (len(pages) if pages else 1):
                        status = "page_unavailable"
                    else:
                        item = dict(
                            original,
                            id=target.key,
                            pid=str(target.illust_id),
                            _page=target.page,
                        )
                        if pages:
                            page_data = pages[target.page - 1]
                            urls = dict(page_data.get("image_urls") or {})
                            item.update(
                                image_urls=urls,
                                meta_pages=[pages[target.page - 1]],
                                meta_single_page={
                                    "original_image_url": urls.get("original", "")
                                },
                            )
                            if target.page > 1:
                                # 作品宽高描述首张；其他页面不能据此拒绝下载或报告尺寸。
                                item.update(
                                    width=page_data.get("width"),
                                    height=page_data.get("height"),
                                )
                        items.append(item)
                        status = "candidate"
            self.target_results.append(target.summary(status))
        return PixivPage(items)

    async def _auto(self) -> PixivPage:
        preferred = self.plugin._cfg_str("preferred_image_source", "Lolicon").lower()
        sources = ("pixiv", "lolicon") if preferred == "pixiv" else ("lolicon", "pixiv")
        if (
            getattr(self.plugin, "lolicon_client", None) is None
            and getattr(self.plugin, "client", None) is None
        ):
            self.plugin._init_client()
        failed = False
        for source in sources:
            client = getattr(
                self.plugin, "client" if source == "pixiv" else "lolicon_client", None
            )
            if client is None or (source == "lolicon" and not client.available):
                continue
            try:
                if source == "pixiv":
                    query = replace(
                        self.query, mode="search" if self.query.tag else "recommended"
                    )
                    page = await client.query_page(query, self.continuation)
                else:
                    method = client.search if self.query.tag else client.random
                    args = (self.query.tag,) if self.query.tag else ()
                    page = PixivPage(
                        await method(
                            *args,
                            count=20,
                            r18=0 if self.policy.general_only_enabled else 2,
                        )
                    )
                self.sampled = source == "lolicon"
                if page.items:
                    # 首次确定来源后，续查固定该来源，不能跨源冒充下一页。
                    if source == "pixiv":
                        self.query = replace(
                            self.query,
                            mode="search" if self.query.tag else "recommended",
                        )
                    return page
            except Exception as exc:
                failed = True
                logger.warning(
                    f"[GetPx] 公开查询图片源失败: source={source} error_type={type(exc).__name__}"
                )
        if failed:
            raise QueryError("upstream_error", "可用图片源请求失败，未获取到候选。")
        return PixivPage([])

    async def next_batch(self) -> list[dict]:
        if self.exhausted:
            return []
        if self.query.tag and await self.plugin._blocked_query_term(
            self.query.tag, self.policy
        ):
            raise QueryError("filtered", "关键词不符合当前内容安全要求。")
        if self.query.mode == "related" and not self.started:
            seed = await self.client().public_detail(self.query.illust_id)
            if seed is None or not await self.filter([seed], dimensions=False):
                raise QueryError("filtered", "关联作品的种子不可用或未通过当前过滤。")
        if self.query.mode == "artwork":
            page = await self._targets()
        elif self.query.mode == "auto":
            page = await self._auto()
        else:
            page = await self.client().query_page(self.query, self.continuation)
        if (
            self.started
            and page.next_params is not None
            and page.next_params == self.continuation
        ):
            raise QueryError("upstream_error", "图片源重复返回相同续页位置。")
        self.started = True
        self.continuation = page.next_params
        self.exhausted = page.next_params is None
        self.raw_count += len(page.items)
        filtered = await self.filter(page.items)
        rejected = {str(item.get("id")) for item in page.items} - {
            str(item.get("id")) for item in filtered
        }
        for target in self.target_results:
            if ArtworkTarget(int(target["id"]), target["page"]).key in rejected:
                target["status"] = "filtered"
        self.filtered_count += len(page.items) - len(filtered)
        return filtered
