from __future__ import annotations


from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import AIOCQHTTP_PLATFORM
from .delivery import log_delivery_error
from .safety import ContentSafetyPolicy, STRICT_CONTENT_SAFETY_POLICY
from .search_result import SearchResult
from .execution import SearchExecution
from .selection import ImageSelection


LOG_PREFIX = "[GetPx]"


class SearchMixin:
    """Search and source-fallback flows."""

    @staticmethod
    def _should_use_forward(
        *, downloaded_count: int, threshold: int, platform_name: str
    ) -> bool:
        return platform_name == AIOCQHTTP_PLATFORM and downloaded_count > threshold

    def _p_unit_cost(self) -> int:
        return self._cfg_int("p_coin_cost", 20, 0, 200)

    def _p_charging_active(self) -> bool:
        return (
            self._p_unit_cost() > 0
            and self._cfg_bool("checkin_enabled", True)
            and getattr(self, "checkin_store", None) is not None
        )

    async def _p_balance_error(
        self, user_id: str, count: int, *, unit_cost: int | None = None
    ) -> str:
        """返回余额不足提示；余额充足或钱包不可用时返回空串。"""
        cost = self._p_unit_cost() if unit_cost is None else unit_cost
        if not user_id or cost <= 0 or count <= 0:
            return ""
        try:
            profile = await self.checkin_store.get_profile(user_id)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} /p 金币预检失败，本次免检放行: "
                f"error_type={type(exc).__name__}"
            )
            return ""
        total = cost * count
        if profile.coins < total:
            return f"金币不足，需要 {total}，当前只有 {profile.coins}。"
        return ""

    def _ensure_client_or_error(self, event: AstrMessageEvent) -> bool:
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            return True
        if getattr(self, "client", None) and self.client.api:
            return True
        self._init_client()
        return bool(
            (getattr(self, "lolicon_client", None) and self.lolicon_client.available)
            or getattr(self, "client", None)
        )

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id() or ''}"

    @staticmethod
    def _source_key(tag: str, source: str) -> str:
        prefix = "search" if tag.strip() else "random"
        return (
            f"{source}:{prefix}:{tag.strip().casefold()}"
            if tag.strip()
            else f"{source}:random"
        )

    async def _fetch_source_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        count: int = 20,
        offset: int = 0,
        aspect_ratio: str = "",
        use_page_cursor: bool = True,
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> tuple[list[dict], int, str]:
        """按首选图片源顺序取图，请求失败或无结果时尝试另一源。"""
        preferred = self._cfg_str("preferred_image_source", "Lolicon").lower()
        if preferred not in ("lolicon", "pixiv"):
            logger.warning(
                f"{LOG_PREFIX} 首选图片源配置无效，使用默认顺序: preferred_source=lolicon"
            )
            preferred = "lolicon"
        sources = ("pixiv", "lolicon") if preferred == "pixiv" else ("lolicon", "pixiv")
        if (
            getattr(self, "lolicon_client", None) is None
            and getattr(self, "client", None) is None
        ):
            self._init_client()
        for source in sources:
            source_key = self._source_key(tag, source)
            if source == "pixiv" and not tag:
                source_key = "pixiv:recommended"
            client = getattr(
                self, "lolicon_client" if source == "lolicon" else "client", None
            )
            if client is None or (source == "lolicon" and not client.available):
                logger.debug(f"{LOG_PREFIX} 图片源不可用，跳过请求: source={source}")
                continue
            try:
                if source == "lolicon":
                    if tag:
                        illusts = await client.search(
                            tag,
                            count=count,
                            aspect_ratio=aspect_ratio,
                            r18=0 if policy.general_only_enabled else 2,
                        )
                    else:
                        illusts = await client.random(
                            count=count,
                            aspect_ratio=aspect_ratio,
                            r18=0 if policy.general_only_enabled else 2,
                        )
                else:
                    page_offset = offset
                    if (
                        use_page_cursor
                        and page_offset == 0
                        and self.image_index is not None
                    ):
                        try:
                            page_offset = await self.image_index.get_page_offset(
                                self._event_scope(event), source_key
                            )
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 读取 Pixiv 分页游标失败: "
                                f"error_type={type(exc).__name__}"
                            )
                    if tag:
                        illusts = await client.search(tag, offset=page_offset)
                    else:
                        illusts = await client.recommended(offset=page_offset)
                if illusts:
                    return illusts, len(illusts), source_key
                logger.info(
                    f"{LOG_PREFIX} 图片源无结果，继续尝试可用来源: source={source}"
                )
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 图片源请求失败，继续尝试可用来源: "
                    f"source={source} tag_configured={'yes' if tag else 'no'} "
                    f"error_type={type(exc).__name__}"
                )
        return [], 0, source_key

    async def _record_image_usage(
        self,
        event: AstrMessageEvent,
        source_key: str,
        illust: dict,
        *,
        feature: str,
        user_id: str = "",
    ) -> None:
        if self.image_index is None or not source_key:
            return
        illust_id = str(illust.get("id") or "")
        if not illust_id:
            return
        try:
            await self.image_index.record_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature=feature,
                user_id=user_id,
            )
        except Exception as exc:
            log_delivery_error(
                exc,
                context=f"illust_id={illust_id}",
                phase="record_usage",
                delivery_status="confirmed",
            )

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
    ):
        """命令展示层；图片发送、清理和结算由共享执行完成。"""
        result = await self._execute_search(event, tag, count_str)
        if result.message:
            yield event.plain_result(result.message)

    async def _execute_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
        *,
        tool: bool = False,
        result: SearchResult | None = None,
        selection_deadline: float | None = None,
        send_mode: str = "auto",
        selection: ImageSelection | None = None,
        candidate_stream=None,
    ) -> SearchResult:
        return await SearchExecution(
            self,
            event,
            tag,
            count_str,
            tool=tool,
            result=result,
            selection_deadline=selection_deadline,
            send_mode=send_mode,
            selection=selection,
            candidate_stream=candidate_stream,
        ).run()
