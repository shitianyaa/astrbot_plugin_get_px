from __future__ import annotations

import random
import re

from astrbot.api.all import logger
from astrbot.api.event import AstrMessageEvent

from .index import ordered_by_unused
from .safety import (
    ContentSafetyPolicy,
    STRICT_CONTENT_SAFETY_POLICY,
    illustration_texts,
    match_safety_term,
    normalized_builtin_terms,
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
        if event is None:
            return STRICT_CONTENT_SAFETY_POLICY
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception as exc:
            logger.error(
                f"{LOG_PREFIX} failed to read group context for content safety: "
                f"error_type={type(exc).__name__}"
            )
            return STRICT_CONTENT_SAFETY_POLICY
        service = getattr(self, "group_safety_service", None)
        if service is None:
            return STRICT_CONTENT_SAFETY_POLICY
        try:
            if group_id:
                value = await service.get_group_policy(group_id)
                identity = {"group_id": group_id}
            else:
                user_id = str(event.get_sender_id() or "").strip()
                if not user_id:
                    return STRICT_CONTENT_SAFETY_POLICY
                value = await service.get_private_policy(user_id)
                identity = {"user_id": user_id}
            general_only = value["general_only_enabled"]
            builtin_terms = value["builtin_terms_enabled"]
            if type(general_only) is not bool or type(builtin_terms) is not bool:
                raise ValueError("invalid group content-safety values")
            custom_terms = value.get("custom_terms", [])
            blacklisted_ids = value.get("blacklisted_illust_ids", [])
            return ContentSafetyPolicy(general_only, builtin_terms, **identity, custom_terms=tuple(custom_terms), blacklisted_illust_ids=tuple(blacklisted_ids))
        except Exception as exc:
            logged_group_id = (
                group_id.replace("\r", "\\r").replace("\n", "\\n")[:128]
            )
            logger.error(
                f"{LOG_PREFIX} failed to read group content-safety policy; "
                f"using strict defaults: group_id={logged_group_id} "
                f"error_type={type(exc).__name__}"
            )
            return STRICT_CONTENT_SAFETY_POLICY

    async def _safety_terms(
        self, policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY
    ) -> set[str]:
        terms = (
            set(normalized_builtin_terms())
            if policy.builtin_terms_enabled
            else {normalize_safety_text(term) for term in policy.custom_terms}
        )
        if not policy.builtin_terms_enabled:
            return terms
        if self.image_index is None:
            return terms
        try:
            terms.update(await self.image_index.get_custom_safety_terms())
        except Exception as exc:
            logger.error(f"{LOG_PREFIX} 读取自定义安全词失败: {type(exc).__name__}")
            raise RuntimeError("内容安全服务暂不可用") from exc
        return terms

    async def _blacklisted_illust_ids(self, policy=STRICT_CONTENT_SAFETY_POLICY) -> set[str]:
        if not policy.builtin_terms_enabled:
            return set(policy.blacklisted_illust_ids)
        if self.image_index is None:
            return set()
        try:
            return set(await self.image_index.get_blacklisted_illust_ids())
        except Exception as exc:
            logger.error(f"{LOG_PREFIX} 读取图片黑名单失败: {type(exc).__name__}")
            raise RuntimeError("内容安全服务暂不可用") from exc

    async def _blocked_query_term(
        self,
        query: str,
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> str:
        return match_safety_term(query, await self._safety_terms(policy))

    @staticmethod
    def _matched_safety_term(illust: dict, terms: set[str]) -> str:
        for value in illustration_texts(illust):
            if matched := match_safety_term(value, terms):
                return matched
        return ""

    async def _blacklist_reason_for_illust(
        self,
        illust: dict,
        illust_id: str = "",
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
    ) -> str:
        illust_id = str(illust_id or illust.get("id") or "")
        if (
            policy.general_only_enabled
            and int(illust.get("x_restrict", 0) or 0) != 0
        ):
            return f"作品 {illust_id or '-'} 不符合内容安全要求"
        matched_tag = self._matched_safety_term(
            illust, await self._safety_terms(policy)
        )
        if matched_tag:
            return f"作品 {illust_id or '-'} 命中内容安全词 {matched_tag}"
        blacklisted_ids = await self._blacklisted_illust_ids(policy)
        for candidate_id in self._illust_blacklist_ids(illust, illust_id):
            if candidate_id in blacklisted_ids:
                return f"作品 {illust_id} 已在黑名单中"
        return ""

    @staticmethod
    def _illust_blacklist_ids(illust: dict, illust_id: str = "") -> set[str]:
        return {
            value
            for value in (
                str(illust_id or ""),
                str(illust.get("id") or ""),
                str(illust.get("pid") or ""),
            )
            if value
        }

    @staticmethod
    def _filter_safe_rating(illusts: list[dict]) -> list[dict]:
        """Only allow Pixiv works explicitly marked as general audience."""
        return [i for i in illusts if int(i.get("x_restrict", 0) or 0) == 0]

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
        safety_terms = await self._safety_terms(policy)
        blacklisted = await self._blacklisted_illust_ids(policy)
        return [
            illust
            for illust in illusts
            if not self._illust_blacklist_ids(illust).intersection(blacklisted)
            and (
                not policy.general_only_enabled
                or int(illust.get("x_restrict", 0) or 0) == 0
            )
            and not self._matched_safety_term(illust, safety_terms)
        ]

    async def _is_blacklisted_illust(self, illust_id: str) -> bool:
        if self.image_index is None or not illust_id:
            return False
        try:
            return await self.image_index.is_blacklisted(illust_id)
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 读取图片黑名单失败: {type(e).__name__}")
            return True

    async def _pick_illusts(
        self,
        event: AstrMessageEvent,
        illusts: list[dict],
        pick_count: int,
        *,
        source_key: str,
        dedupe_enabled: bool = True,
        raw_count: int = 0,
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
            return []

        ordered = ordered_by_unused(illusts, used_ids)
        fresh = [i for i in ordered if str(i.get("id") or "") not in used_ids]
        repeated = [i for i in ordered if str(i.get("id") or "") in used_ids]

        # 若整页全被用过，推进分页游标供下次翻页
        if not fresh and raw_count > 0:
            try:
                await self.image_index.advance_page_offset(scope, source_key, raw_count)
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} 分页游标更新失败: "
                    f"error_type={type(e).__name__}"
                )

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
            except Exception:
                return chosen
            if claimed:
                chosen.append(illust)
        return chosen
