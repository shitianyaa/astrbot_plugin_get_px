from __future__ import annotations

import random
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .index import ordered_by_unused
from .usage import usage_id
from .safety import (
    ContentSafetyPolicy,
    STRICT_CONTENT_SAFETY_POLICY,
    matched_illustration_term,
    match_safety_term,
    normalize_safety_text,
)


LOG_PREFIX = "[GetPx]"


class FiltersMixin:
    """Pixiv content filters, blacklist checks and deduplicated selection."""

    @staticmethod
    def _split_config_tags(value: object) -> list[str]:
        if not isinstance(value, str):
            return []
        return [
            tag.strip()
            for tag in re.split(r"[,\uFF0C、;\uFF1B\r\n]+", value)
            if tag.strip()
        ]

    async def _content_safety_policy(
        self, event: AstrMessageEvent | None
    ) -> ContentSafetyPolicy:
        service = getattr(self, "safety_service", None)
        if service is None:
            raise RuntimeError("内容安全服务暂不可用")
        if event is None:
            return await service.global_policy()
        try:
            return await service.resolve(event.unified_msg_origin)
        except Exception as exc:
            logger.error(
                f"{LOG_PREFIX} 安全分组读取失败，本次请求已拒绝: "
                f"error_type={type(exc).__name__}"
            )
            raise RuntimeError("内容安全服务暂不可用") from exc

    async def _blocked_query_term(
        self, query: str, policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> str:
        terms = {normalize_safety_text(term) for term in policy.custom_terms}
        normalized = normalize_safety_text(query)
        if policy.match_mode == "严格":
            return next((term for term in sorted(terms) if term in normalized), "")
        return match_safety_term(query, terms)

    @staticmethod
    def _matched_safety_term(illust: dict, policy: ContentSafetyPolicy) -> str:
        return matched_illustration_term(illust, policy)

    async def _blacklist_reason_for_illust(
        self,
        illust: dict,
        illust_id: str = "",
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> str:
        illust_id = str(illust_id or illust.get("id") or "")
        if policy.general_only_enabled and int(illust.get("x_restrict", 0) or 0) != 0:
            return f"作品 {illust_id or '-'} 不符合内容安全要求"
        if self._matched_safety_term(illust, policy):
            return f"作品 {illust_id or '-'} 命中屏蔽关键词"
        if self._illust_blacklist_ids(illust, illust_id).intersection(policy.blacklisted_illust_ids):
            return f"作品 {illust_id} 已在黑名单中"
        return ""

    @staticmethod
    def _illust_blacklist_ids(illust: dict, illust_id: str = "") -> set[str]:
        return {
            value
            for value in (
                str(illust_id or "").split(":", 1)[0],
                str(illust.get("id") or "").split(":", 1)[0],
                str(illust.get("pid") or ""),
            )
            if value
        }

    @staticmethod
    def _filter_manga(illusts: list[dict]) -> list[dict]:
        """Filter out every Pixiv manga item."""
        return [il for il in illusts if il.get("type") != "manga"]

    async def _filter_blacklisted_illusts(
        self,
        illusts: list[dict],
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> list[dict]:
        if not illusts:
            return illusts
        blacklisted = set(policy.blacklisted_illust_ids)
        return [
            illust
            for illust in illusts
            if not self._illust_blacklist_ids(illust).intersection(blacklisted)
            and (
                not policy.general_only_enabled
                or int(illust.get("x_restrict", 0) or 0) == 0
            )
            and not self._matched_safety_term(illust, policy)
        ]

    async def _pick_illusts(
        self,
        event: AstrMessageEvent,
        illusts: list[dict],
        pick_count: int,
        *,
        source_key: str,
        dedupe_enabled: bool = True,
        raw_count: int = 0,
        ordered: bool = False,
        allow_repeat: bool = False,
        owner: str = "",
    ) -> list[dict]:
        if self.image_index is None:
            return illusts[:pick_count] if ordered else random.sample(illusts, pick_count)

        scope = self._event_scope(event)
        try:
            used_ids = (
                await self.image_index.get_used_illust_ids(scope, source_key)
                if dedupe_enabled else set()
            )
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX} 读取发图去重索引失败: "
                f"error_type={type(e).__name__}"
            )
            return []

        by_usage = ordered_by_unused(illusts, used_ids)
        fresh = [i for i in by_usage if usage_id(i.get("id")) not in used_ids]
        repeated = [i for i in by_usage if usage_id(i.get("id")) in used_ids]

        candidates = (
            list(illusts) if ordered else
            random.sample(fresh, len(fresh)) + random.sample(repeated, len(repeated))
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
                    source_key=source_key + (":explicit_pending" if allow_repeat else ""),
                    illust_id=illust_id,
                    feature="normal_pending",
                    user_id=user_id,
                    ignore_history=allow_repeat or not dedupe_enabled,
                    owner=owner,
                )
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 图片占用失败: error_type={type(exc).__name__}")
                return chosen
            if claimed:
                chosen.append(illust)
        return chosen
