from __future__ import annotations

import random
import re

from astrbot.api.all import logger
from astrbot.api.event import AstrMessageEvent

from .index import ordered_by_unused


LOG_PREFIX = "[GetPx]"
DEFAULT_BLACKLIST_TAGS = "furry,裸体,全裸,触手,露出,nsfw"


class FiltersMixin:
    """Pixiv content filters, blacklist checks and deduplicated selection."""

    @staticmethod
    def _split_config_tags(value: object) -> list[str]:
        if not isinstance(value, str):
            return []
        return [
            tag.strip()
            for tag in re.split(r"[,，、;；\r\n]+", value)
            if tag.strip()
        ]

    def _blacklist_tags(self) -> set[str]:
        configured = self._split_config_tags(self._cfg_str("blacklist_tags", ""))
        tags = configured or self._split_config_tags(DEFAULT_BLACKLIST_TAGS)
        return {tag.casefold() for tag in tags}

    @staticmethod
    def _illust_tag_names(illust: dict) -> set[str]:
        names: set[str] = set()
        for tag in illust.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            for key in ("name", "translated_name"):
                value = str(tag.get(key) or "").strip()
                if value:
                    names.add(value.casefold())
        return names

    def _matched_blacklist_tag(self, illust: dict) -> str:
        matched = self._illust_tag_names(illust) & self._blacklist_tags()
        return sorted(matched)[0] if matched else ""

    async def _blacklist_reason_for_illust(
        self, illust: dict, illust_id: str = ""
    ) -> str:
        illust_id = str(illust_id or illust.get("id") or "")
        matched_tag = self._matched_blacklist_tag(illust)
        if matched_tag:
            return f"作品 {illust_id or '-'} 命中拉黑标签 {matched_tag}"
        if await self._is_blacklisted_illust(illust_id):
            return f"作品 {illust_id} 已在黑名单中"
        return ""

    @staticmethod
    def _filter_r18(illusts: list[dict], mode: int) -> list[dict]:
        """根据 R18 模式过滤作品。mode: 0=仅非R18, 1=仅R18, 2=混合。"""

        def keep(illust: dict) -> bool:
            xr = int(illust.get("x_restrict", 0) or 0)
            if mode == 0:
                return xr == 0
            if mode == 1:
                return xr > 0
            return True

        return [i for i in illusts if keep(i)]


    @staticmethod
    def _filter_manga(illusts: list[dict]) -> list[dict]:
        """Filter out every Pixiv manga item."""
        return [il for il in illusts if il.get("type") != "manga"]


    async def _filter_blacklisted_illusts(self, illusts: list[dict]) -> list[dict]:
        if not illusts:
            return illusts
        blacklist_tags = self._blacklist_tags()
        blacklisted: set[str] = set()
        if self.image_index is None and not blacklist_tags:
            return illusts
        try:
            if self.image_index is not None:
                blacklisted = await self.image_index.get_blacklisted_illust_ids()
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取图片黑名单失败: {e}")
        if not blacklisted and not blacklist_tags:
            return illusts
        return [
            illust
            for illust in illusts
            if str(illust.get("id") or "") not in blacklisted
            and not self._matched_blacklist_tag(illust)
        ]


    async def _is_blacklisted_illust(self, illust_id: str) -> bool:
        if self.image_index is None or not illust_id:
            return False
        try:
            return await self.image_index.is_blacklisted(illust_id)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取图片黑名单失败: {e}")
            return False

    async def _pick_illusts(
        self,
        event: AstrMessageEvent,
        illusts: list[dict],
        pick_count: int,
        *,
        tag: str,
        ranking_mode: str,
        dedupe_enabled: bool = True,
        raw_count: int = 0,
    ) -> list[dict]:
        if not dedupe_enabled or self.image_index is None:
            return random.sample(illusts, pick_count)

        source_key = self._source_key(tag, ranking_mode)
        scope = self._event_scope(event)
        try:
            used_ids = await self.image_index.get_used_illust_ids(
                scope, source_key
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取当天发图索引失败: {e}")
            return []

        ordered = ordered_by_unused(illusts, used_ids)
        fresh = [i for i in ordered if str(i.get("id") or "") not in used_ids]
        repeated = [i for i in ordered if str(i.get("id") or "") in used_ids]

        # 若整页全被用过，推进分页游标供下次翻页
        if not fresh and raw_count > 0:
            try:
                await self.image_index.advance_page_offset(
                    scope, source_key, raw_count
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 分页游标更新失败: {e}")

        candidates = random.sample(fresh, len(fresh)) + random.sample(
            repeated, len(repeated)
        )
        chosen: list[dict] = []
        user_id = str(event.get_sender_id() or "")
        for illust in candidates:
            if len(chosen) >= pick_count:
                break
            illust_id = str(illust.get("id") or "")
            if not illust_id:
                continue
            try:
                claimed = await self.image_index.claim_usage(
                    scope=scope,
                    source_key=source_key,
                    illust_id=illust_id,
                    feature="normal_pending",
                    user_id=user_id,
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 占用当天发图索引失败: {e}")
                return chosen
            if claimed:
                chosen.append(illust)
        return chosen
