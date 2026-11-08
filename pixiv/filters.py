from __future__ import annotations

import asyncio
import random
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .index import ordered_by_unused
from .pagination import SelectionIndexError
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
    ) -> list[dict]:
        if not dedupe_enabled or self.image_index is None:
            return random.sample(illusts, pick_count)

        scope = self._event_scope(event)
        try:
            used_ids = await self.image_index.get_used_illust_ids(scope, source_key)
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX} 读取发图去重索引失败: "
                f"error_type={type(e).__name__}"
            )
            raise SelectionIndexError("读取去重索引失败") from e

        ordered = ordered_by_unused(illusts, used_ids)
        fresh = [i for i in ordered if str(i.get("id") or "") not in used_ids]
        repeated = [i for i in ordered if str(i.get("id") or "") in used_ids]

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
            claim_task = asyncio.ensure_future(self.image_index.claim_usage(
                scope=scope, source_key=source_key, illust_id=illust_id,
                feature="normal_pending", user_id=user_id,
            ))
            try:
                # SQLite 线程可能在外部取消后完成写入，先等占用结果再释放。
                claimed = await asyncio.shield(claim_task)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    try:
                        if await claim_task:
                            chosen.append(illust)
                    except Exception as claim_exc:
                        logger.warning(f"{LOG_PREFIX} 取消后图片占用失败: error_type={type(claim_exc).__name__}")
                for claimed_illust in chosen:
                    try:
                        await self.image_index.release_usage(
                            scope=scope, source_key=source_key,
                            illust_id=str(claimed_illust["id"]), feature="normal_pending",
                        )
                    except Exception as release_exc:
                        logger.warning(f"{LOG_PREFIX} 选图占用释放失败: error_type={type(release_exc).__name__}")
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if not isinstance(exc, Exception):
                    raise
                raise SelectionIndexError("图片占用失败") from exc
            if claimed:
                chosen.append(illust)
        return chosen
