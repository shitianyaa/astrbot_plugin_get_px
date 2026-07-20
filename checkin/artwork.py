from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import random
import time

from astrbot.api.all import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.star_tools import StarTools

from .models import CheckinRecord
from .background import (
    CHECKIN_ARTWORK_TARGET_RATIO,
    CHECKIN_ARTWORK_TOLERANCE,
    filter_illusts_by_aspect_ratio,
)
from .card import (
    CHECKIN_CARD_HEIGHT,
    CHECKIN_CARD_WIDTH,
    CardBackground,
    build_checkin_card_data,
    get_checkin_card_template,
)
from .cache import is_valid_card_jpeg
from .quality import (
    CHECKIN_JPEG_QUALITY,
    DEFAULT_CHECKIN_RENDER_TIER,
    CheckinRenderTier,
    checkin_render_fallbacks,
    get_checkin_render_tier,
    normalize_checkin_render_tier,
)


_CHECKIN_BACKGROUND_MODE_LABELS = {
    "pixiv_daily": "在线图片",
    "custom": "自定义背景",
    "fallback": "占位图",
}


def _checkin_background_mode_label(background: CardBackground | None) -> str:
    mode = getattr(background, "mode", "") or "none"
    return _CHECKIN_BACKGROUND_MODE_LABELS.get(str(mode), str(mode))

try:
    from ..pixiv.index import ordered_by_unused
    from ..pixiv.downloader import cleanup
except ImportError:  # Direct imports used by the test suite.
    from pixiv.index import ordered_by_unused
    from pixiv.downloader import cleanup


LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
CHECKIN_BACKGROUND_PAGE_ATTEMPTS = 5
CHECKIN_PREVIEW_BACKGROUND_TTL_SECONDS = 300.0


class CheckinArtworkMixin:
    """Render cards and select, claim, restore and release artwork."""

    async def _record_checkin_background(
        self, event: AstrMessageEvent, background: CardBackground | None
    ) -> None:
        if (
            background is None
            or background.mode != "pixiv_daily"
            or not background.source
            or not background.illust_id
        ):
            return
        illust = dict(background.illust or {})
        illust.setdefault("id", background.illust_id)
        illust.setdefault("title", background.title)
        illust.setdefault("user", {"name": background.author})
        await self._record_image_usage(
            event,
            background.source,
            illust,
            feature="checkin",
            user_id=str(event.get_sender_id() or ""),
        )

    async def _restore_checkin_background(
        self,
        event: AstrMessageEvent,
        record: CheckinRecord,
    ) -> CardBackground:
        saved = self._checkin_background_from_record(record)
        if record.background_mode == "custom":
            custom_path = self._resolve_custom_background_path(
                self._cfg_str("checkin_custom_background", "")
            )
            if custom_path is not None:
                logger.debug(f"{LOG_PREFIX} 签到背景恢复完成: mode=custom")
                return replace(saved, image_path=str(custom_path), mode="custom")
            logger.warning(
                f"{LOG_PREFIX} 签到背景恢复失败: mode=custom "
                f"reason=custom_file_unavailable"
            )
            return replace(saved, mode="fallback")

        if not record.background_illust_id:
            logger.debug(
                f"{LOG_PREFIX} 签到背景恢复跳过: reason=no_persisted_illust"
            )
            return replace(saved, mode="fallback")
        source = str(record.background_source or "")
        detail_id_text = str(record.background_illust_id)
        detail_page = 0
        if source.startswith("lolicon:"):
            detail_id_text, separator, page_text = detail_id_text.partition(":")
            if separator:
                try:
                    detail_page = int(page_text)
                except ValueError:
                    logger.warning(
                        f"{LOG_PREFIX} 签到背景恢复失败: "
                        f"reason=invalid_lolicon_page"
                    )
                    return replace(saved, mode="fallback")
            if not self._cfg_str("pixiv_refresh_token"):
                logger.warning(
                    f"{LOG_PREFIX} 签到背景恢复失败: "
                    f"reason=lolicon_restore_requires_pixiv_token"
                )
                return replace(saved, mode="fallback")
        try:
            detail_id = int(detail_id_text)
        except ValueError:
            logger.warning(
                f"{LOG_PREFIX} 签到背景恢复失败: reason=invalid_illust_id"
            )
            return replace(saved, mode="fallback")
        if getattr(self, "client", None) is None:
            self._init_client()
        if self.client is None:
            logger.warning(
                f"{LOG_PREFIX} 签到背景恢复失败: reason=pixiv_client_unavailable"
            )
            return replace(saved, mode="fallback")

        try:
            illust = await self.client.illust_detail(detail_id)
            if not illust:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景恢复失败: reason=detail_not_found "
                    f"illust_id={record.background_illust_id}"
                )
                return replace(saved, mode="fallback")
            if source.startswith("lolicon:"):
                illust = self._select_pixiv_detail_page(illust, detail_page)
                if not illust:
                    logger.warning(
                        f"{LOG_PREFIX} 签到背景恢复失败: "
                        f"reason=detail_page_not_found "
                        f"illust_id={record.background_illust_id}"
                    )
                    return replace(saved, mode="fallback")
            if await self._blacklist_reason_for_illust(
                illust, record.background_illust_id
            ):
                logger.warning(
                    f"{LOG_PREFIX} 签到背景恢复被内容安全策略拒绝: "
                    f"illust_id={record.background_illust_id}"
                )
                return replace(saved, mode="fallback")
            if not filter_illusts_by_aspect_ratio(
                [illust],
                CHECKIN_ARTWORK_TARGET_RATIO,
                CHECKIN_ARTWORK_TOLERANCE,
            ):
                logger.warning(
                    f"{LOG_PREFIX} 签到背景恢复拒绝非 3:4 作品 {record.background_illust_id}"
                )
                return replace(saved, mode="fallback")
            if source.startswith("lolicon:"):
                illust = dict(illust)
                illust["_source"] = "lolicon"

            timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
            saved_quality = str(getattr(record, "background_quality", "") or "")
            background_quality = saved_quality or get_checkin_render_tier(
                self._record_checkin_render_tier(record)
            ).background_quality
            path, actual_quality, file_size = await self.downloader.download_for_send(
                illust,
                background_quality,
                timeout=timeout_sec,
                downgrade_limit_bytes=0,
                log_context=f"[签到背景恢复] 作品 {record.background_illust_id}",
            )
            logger.debug(
                f"{LOG_PREFIX} 签到背景恢复完成: mode=pixiv_daily "
                f"source={source.partition(':')[0] or 'unknown'} "
                f"illust_id={record.background_illust_id} quality={actual_quality}"
            )
            return CardBackground(
                image_path=path,
                mode="pixiv_daily",
                source=record.background_source,
                illust_id=record.background_illust_id,
                title=record.background_title,
                author=record.background_author,
                illust=illust,
                quality=actual_quality,
                file_size=file_size,
            )
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX} 签到背景恢复失败，使用占位图: "
                f"illust_id={record.background_illust_id} "
                f"error_type={type(e).__name__}"
            )
            return replace(saved, mode="fallback")

    @staticmethod
    def _select_pixiv_detail_page(illust: dict, page: int) -> dict | None:
        """把 Pixiv 详情转换为 Lolicon 记录所指向的具体页面。"""
        if page < 0:
            return None
        meta_pages = illust.get("meta_pages") or []
        if not meta_pages:
            return illust if page == 0 else None
        if page >= len(meta_pages):
            return None
        page_data = meta_pages[page] or {}
        page_urls = page_data.get("image_urls") or {}
        selected = dict(illust)
        selected["id"] = f"{illust['id']}:{page}"
        selected["meta_single_page"] = {
            "original_image_url": str(page_urls.get("original") or "")
        }
        selected["image_urls"] = dict(page_urls)
        selected["meta_pages"] = [page_data]
        return selected

    async def _render_checkin_card(
        self,
        event: AstrMessageEvent,
        *,
        profile,
        record,
        background: CardBackground | None,
        bot_name: str,
        user_title: str = "",
        render_tier: str | None = None,
    ) -> str:
        avatar_url = (
            self._checkin_avatar_url(event)
            if self._cfg_bool("checkin_avatar_enabled", True)
            else ""
        )
        width = CHECKIN_CARD_WIDTH
        height = CHECKIN_CARD_HEIGHT
        render_spec = get_checkin_render_tier(
            render_tier or self._configured_checkin_render_tier()
        )
        # 视图模型构建含背景图 Data URL 编码（读文件 + base64），放入线程池
        data = await asyncio.to_thread(
            build_checkin_card_data,
            profile=profile,
            record=record,
            bot_name=bot_name,
            avatar_url=avatar_url,
            background=background,
            user_title=user_title,
            width=width,
            height=height,
            background_refresh_cost=self._cfg_int(
                "checkin_background_refresh_cost", 100, 0, 500
            ),
        )
        options = {
            "full_page": False,
            "type": "jpeg",
            "quality": CHECKIN_JPEG_QUALITY,
            "clip": {"x": 0, "y": 0, "width": width, "height": height},
            "viewport": {"width": width, "height": height},
            "animations": "disabled",
        }
        if render_spec.scale_level is not None:
            options["device_scale_factor_level"] = render_spec.scale_level
        return await self.html_render(
            get_checkin_card_template(getattr(record, "theme_id", "default")),
            data,
            return_url=False,
            options=options,
        )

    def _configured_checkin_render_tier(self) -> str:
        return normalize_checkin_render_tier(
            self._cfg_str("checkin_card_quality_tier", DEFAULT_CHECKIN_RENDER_TIER)
        )

    @staticmethod
    def _record_checkin_render_tier(record: CheckinRecord) -> str:
        return normalize_checkin_render_tier(
            getattr(record, "render_tier", DEFAULT_CHECKIN_RENDER_TIER)
        )

    @staticmethod
    def _cache_get_for_tier(cache, date_key: str, cache_key: str, spec):
        return cache.get(
            date_key,
            cache_key,
            expected_size=spec.expected_size,
        )

    @staticmethod
    async def _cache_store_for_tier(
        cache,
        date_key: str,
        cache_key: str,
        renderer,
        spec: CheckinRenderTier,
    ):
        return await cache.store(
            date_key,
            cache_key,
            renderer,
            expected_size=spec.expected_size,
        )

    async def _get_cached_checkin_card(
        self,
        event: AstrMessageEvent,
        *,
        cache,
        profile,
        record,
        background: CardBackground | None,
        bot_name: str,
        user_title: str,
        preferred_tier: str,
    ) -> tuple[Path | None, str]:
        for spec in checkin_render_fallbacks(preferred_tier):
            cache_key = await asyncio.to_thread(
                self._checkin_card_cache_key,
                event,
                profile=profile,
                record=record,
                background=background,
                bot_name=bot_name,
                user_title=user_title,
                render_tier=spec.name,
            )
            cached = await asyncio.to_thread(
                self._cache_get_for_tier,
                cache,
                record.date_key,
                cache_key,
                spec,
            )
            if cached is not None:
                logger.debug(
                    f"{LOG_PREFIX} 签到卡缓存命中: 画质={spec.name} "
                    f"日期={record.date_key} 输出尺寸={spec.expected_size[0]}x{spec.expected_size[1]}"
                )
                return Path(cached), spec.name
            logger.debug(
                f"{LOG_PREFIX} 签到卡缓存未命中: 画质={spec.name} "
                f"日期={record.date_key} 输出尺寸={spec.expected_size[0]}x{spec.expected_size[1]}"
            )
        return None, normalize_checkin_render_tier(preferred_tier)

    async def _render_checkin_card_with_fallback(
        self,
        event: AstrMessageEvent,
        *,
        profile,
        record,
        background: CardBackground | None,
        bot_name: str,
        user_title: str = "",
        preferred_tier: str,
        cache=None,
    ) -> tuple[Path, str]:
        last_error: Exception | None = None
        fallback_specs = checkin_render_fallbacks(preferred_tier)
        for tier_index, spec in enumerate(fallback_specs):
            renderer_source_path = ""
            render_succeeded = False
            started_at = time.monotonic()

            async def render_card() -> str:
                nonlocal renderer_source_path
                renderer_source_path = await self._render_checkin_card(
                    event,
                    profile=profile,
                    record=record,
                    background=background,
                    bot_name=bot_name,
                    user_title=user_title,
                    render_tier=spec.name,
                )
                return renderer_source_path

            try:
                logger.debug(
                    f"{LOG_PREFIX} 签到卡开始渲染: 画质={spec.name} "
                    f"输出尺寸={spec.expected_size[0]}x{spec.expected_size[1]} "
                    f"背景模式={_checkin_background_mode_label(background)}"
                )
                if cache is None:
                    card_path = Path(await render_card())
                    if not is_valid_card_jpeg(card_path, spec.expected_size):
                        width, height = spec.expected_size
                        raise ValueError(
                            f"renderer output must be a valid {width}x{height} JPEG"
                        )
                else:
                    cache_key = await asyncio.to_thread(
                        self._checkin_card_cache_key,
                        event,
                        profile=profile,
                        record=record,
                        background=background,
                        bot_name=bot_name,
                        user_title=user_title,
                        render_tier=spec.name,
                    )
                    card_path = Path(
                        await self._cache_store_for_tier(
                            cache,
                            record.date_key,
                            cache_key,
                            render_card,
                            spec,
                        )
                    )
                render_succeeded = True
                logger.info(
                    f"{LOG_PREFIX} 签到卡渲染完成：画质={spec.name} "
                    f"输出尺寸={spec.expected_size[0]}x{spec.expected_size[1]} "
                    f"耗时={int((time.monotonic() - started_at) * 1000)}ms"
                )
                return card_path, spec.name
            except Exception as exc:
                last_error = exc
                has_lower_tier = tier_index + 1 < len(fallback_specs)
                logger.warning(
                    f"{LOG_PREFIX} 签到卡渲染失败: 画质={spec.name} "
                    f"输出尺寸={spec.expected_size[0]}x{spec.expected_size[1]} "
                    f"是否降档={'是' if has_lower_tier else '否'} "
                    f"错误类型={type(exc).__name__}"
                )
            finally:
                if cache is not None or not render_succeeded:
                    cleanup(renderer_source_path)
        raise RuntimeError("all check-in card render tiers failed") from last_error

    async def _prepare_checkin_background(
        self,
        event: AstrMessageEvent,
        record,
        *,
        claim_usage: bool = True,
        refresh_preview: bool = False,
        render_tier: str | None = None,
    ) -> CardBackground | None:
        mode = self._cfg_str("checkin_background_mode", "pixiv_daily") or "pixiv_daily"
        if mode == "custom":
            custom_path = self._resolve_custom_background_path(
                self._cfg_str("checkin_custom_background", "")
            )
            if custom_path:
                logger.debug(f"{LOG_PREFIX} 签到背景选择完成: mode=custom")
                return CardBackground(
                    image_path=str(custom_path),
                    mode="custom",
                    source="custom",
                )
            logger.warning(f"{LOG_PREFIX} 签到自定义背景不可用\uff0c回退在线图片源背景")
        elif mode != "pixiv_daily":
            mode = "pixiv_daily"
        preview_nonce = 0
        preview_excluded_ids: set[str] = set()
        if refresh_preview:
            preview_nonce = int(getattr(self, "_checkin_preview_sequence", 0)) + 1
            self._checkin_preview_sequence = preview_nonce
            recent_map = getattr(self, "_checkin_preview_background_ids", None)
            if recent_map is None:
                recent_map = {}
                self._checkin_preview_background_ids = recent_map
            now_monotonic = time.monotonic()
            for recent_user_id, recent_items in list(recent_map.items()):
                active_items = [
                    (illust_id, created_at)
                    for illust_id, created_at in recent_items
                    if now_monotonic - created_at
                    < CHECKIN_PREVIEW_BACKGROUND_TTL_SECONDS
                ]
                if active_items:
                    recent_map[recent_user_id] = active_items[-20:]
                else:
                    recent_map.pop(recent_user_id, None)
            active_recent = [
                (illust_id, created_at)
                for illust_id, created_at in recent_map.get(record.user_id, ())
            ]
            recent_map[record.user_id] = active_recent
            preview_excluded_ids.update(item[0] for item in active_recent)

        pixiv_bg = await self._download_checkin_pixiv_background(
            event,
            record,
            claim_usage=claim_usage,
            preview_nonce=preview_nonce,
            preview_excluded_ids=preview_excluded_ids,
            background_quality=get_checkin_render_tier(
                render_tier or self._configured_checkin_render_tier()
            ).background_quality,
        )
        if pixiv_bg is not None:
            if refresh_preview and pixiv_bg.illust_id:
                recent = list(
                    self._checkin_preview_background_ids.get(record.user_id, ())
                )
                recent.append((pixiv_bg.illust_id, time.monotonic()))
                self._checkin_preview_background_ids[record.user_id] = recent[-20:]
            return pixiv_bg
        logger.info(
            f"{LOG_PREFIX} 签到背景选择失败，使用占位图: "
            f"reason=no_available_online_background"
        )
        return CardBackground(mode="fallback", source="fallback")

    def _resolve_custom_background_path(self, value: str) -> Path | None:
        raw = str(value or "").strip().strip('"')
        if not raw:
            return None
        path = Path(raw)
        if not path.is_absolute():
            path = Path(StarTools.get_data_dir(PLUGIN_NAME)) / raw
        try:
            resolved = path.resolve()
        except OSError as exc:
            logger.debug(
                f"{LOG_PREFIX} 签到自定义背景不可用: stage=resolve "
                f"error_type={type(exc).__name__}"
            )
            return None
        if not resolved.is_file():
            logger.debug(
                f"{LOG_PREFIX} 签到自定义背景不可用: reason=file_not_found"
            )
            return None
        if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            logger.debug(
                f"{LOG_PREFIX} 签到自定义背景不可用: reason=unsupported_format"
            )
            return None
        try:
            from PIL import Image as PILImage
        except ImportError:
            logger.warning(f"{LOG_PREFIX} 未安装 Pillow\uff0c跳过背景完整性校验")
            return resolved
        try:
            with PILImage.open(resolved) as img:
                img.verify()
        except Exception:
            logger.debug(
                f"{LOG_PREFIX} 签到自定义背景不可用: reason=corrupt_or_unreadable"
            )
            return None
        return resolved

    async def _download_checkin_pixiv_background(
        self,
        event: AstrMessageEvent,
        record,
        *,
        claim_usage: bool = True,
        preview_nonce: int = 0,
        preview_excluded_ids: set[str] | None = None,
        background_quality: str = "medium",
        _selected_tag: str | None = None,
    ) -> CardBackground | None:
        if _selected_tag is None:
            tag_config = self._cfg_str("checkin_background_tag", "")
            for selected_tag in self._checkin_background_tag_candidates(tag_config):
                background = await self._download_checkin_pixiv_background(
                    event,
                    record,
                    claim_usage=claim_usage,
                    preview_nonce=preview_nonce,
                    preview_excluded_ids=preview_excluded_ids,
                    background_quality=background_quality,
                    _selected_tag=selected_tag,
                )
                if background is not None:
                    return background
            return None

        selected_tag = _selected_tag

        source_key = ""
        used_ids: set[str] = set(preview_excluded_ids or ())
        used_source_key = ""
        illusts: list[dict] = []
        raw_count = 0
        transient_offset = 0

        for page_attempt in range(1, CHECKIN_BACKGROUND_PAGE_ATTEMPTS + 1):
            try:
                illusts, raw_count, source_key = await self._fetch_source_candidates(
                    event,
                    selected_tag,
                    count=20,
                    offset=transient_offset if preview_nonce else 0,
                    aspect_ratio="vertical",
                    use_page_cursor=not preview_nonce,
                )
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景请求失败: "
                    f"tag_configured={'yes' if selected_tag else 'no'} "
                    f"error_type={type(e).__name__}"
                )
                return None
            if preview_nonce:
                transient_offset += raw_count
            if source_key != used_source_key:
                used_ids = set(preview_excluded_ids or ())
                used_ids.update(
                    await self._checkin_background_used_ids(event, source_key)
                )
                used_source_key = source_key
            if not illusts:
                return None

            if self._cfg_bool("filter_manga", True):
                illusts = self._filter_manga(illusts)
            try:
                illusts = await self._filter_blacklisted_illusts(illusts)
            except RuntimeError as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景安全检查不可用，使用占位图: "
                    f"error_type={type(exc).__name__}"
                )
                return None
            illusts = filter_illusts_by_aspect_ratio(
                illusts,
                CHECKIN_ARTWORK_TARGET_RATIO,
                CHECKIN_ARTWORK_TOLERANCE,
            )
            if not illusts:
                if preview_nonce:
                    continue
                if self.image_index is None:
                    return None
                try:
                    await self.image_index.advance_page_offset(
                        self._event_scope(event), source_key, raw_count
                    )
                    logger.info(
                        f"{LOG_PREFIX} 签到背景第 {page_attempt} 页无符合 3:4 的竖向作品\uff0c切换下一页"
                    )
                except Exception as e:
                    logger.warning(
                        f"{LOG_PREFIX} 签到背景分页游标更新失败: "
                        f"error_type={type(e).__name__}"
                    )
                    return None
                continue

            ordered = ordered_by_unused(illusts, used_ids)
            fresh = [
                illust
                for illust in ordered
                if str(illust.get("id") or "") not in used_ids
            ]
            if fresh:
                illusts = fresh
                break
            if preview_nonce:
                continue
            if self.image_index is None:
                logger.info(
                    f"{LOG_PREFIX} 签到背景候选均已在去重窗口内使用，跳过图片源背景"
                )
                return None

            try:
                await self.image_index.advance_page_offset(
                    self._event_scope(event), source_key, raw_count
                )
                logger.info(
                    f"{LOG_PREFIX} 签到背景第 {page_attempt} 页候选均已使用\uff0c切换下一页"
                )
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景分页游标更新失败: "
                    f"error_type={type(e).__name__}"
                )
                return None
        else:
            logger.info(
                f"{LOG_PREFIX} 签到背景连续 {CHECKIN_BACKGROUND_PAGE_ATTEMPTS} 页无可用竖向作品"
            )
            return None

        seed_text = f"checkin-bg|{record.date_key}|{record.user_id}|{source_key}"
        if preview_nonce:
            seed_text += f"|{preview_nonce}"
        seed = int.from_bytes(
            hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big"
        )
        start = seed % len(illusts)
        ordered = illusts[start:] + illusts[:start]
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        for idx, illust in enumerate(ordered[:8], 1):
            illust_id = str(illust.get("id") or "")
            if not illust_id:
                continue
            try:
                reason = await self._blacklist_reason_for_illust(illust, illust_id)
            except RuntimeError as exc:
                # 自定义安全词/黑名单读取失败时 fail-closed：不放过该候选，
                # 跳过去试下一个；若所有候选都不可用则回退占位图。
                logger.warning(
                    f"{LOG_PREFIX} 签到背景安全检查不可用，跳过作品: "
                    f"illust_id={illust_id} error_type={type(exc).__name__}"
                )
                continue
            if reason:
                logger.debug(
                    f"{LOG_PREFIX} 签到背景跳过: reason=content_policy "
                    f"illust_id={illust_id}"
                )
                continue
            claimed = False
            if claim_usage:
                claimed = await self._claim_checkin_background_usage(
                    event, source_key, illust_id
                )
                if not claimed:
                    logger.debug(
                        f"{LOG_PREFIX} 签到背景跳过\uff1a作品 {illust_id} 已被其他签到占用"
                    )
                    continue
            title = illust.get("title", "无标题")
            background_ready = False
            try:
                path, actual_q, file_size = await self.downloader.download_for_send(
                    illust,
                    background_quality,
                    timeout=timeout_sec,
                    downgrade_limit_bytes=0,
                    log_context=f"[签到背景 {idx}] 作品 {illust_id}",
                )
                author = str((illust.get("user") or {}).get("name") or "")
                background = CardBackground(
                    image_path=path,
                    mode="pixiv_daily",
                    source=source_key,
                    illust_id=illust_id,
                    title=str(title or ""),
                    author=author,
                    illust=illust,
                    quality=actual_q,
                    file_size=file_size,
                )
                background_ready = True
                logger.debug(
                    f"{LOG_PREFIX} 签到背景选择完成: mode=pixiv_daily "
                    f"source={source_key.partition(':')[0] or 'unknown'} "
                    f"illust_id={illust_id} quality={actual_q} file_size={file_size}"
                )
                return background
            except asyncio.TimeoutError:
                logger.debug(
                    f"{LOG_PREFIX} 签到背景候选跳过: reason=timeout "
                    f"illust_id={illust_id}"
                )
            except Exception as e:
                logger.debug(
                    f"{LOG_PREFIX} 签到背景候选跳过: reason=download_error "
                    f"illust_id={illust_id} "
                    f"error_type={type(e).__name__}"
                )
            finally:
                if claimed and not background_ready:
                    await self._release_checkin_background_usage(
                        event, source_key, illust_id
                    )
        return None

    def _checkin_background_tag_candidates(self, tag_config: object) -> list[str]:
        tags = self._split_config_tags(tag_config)
        if not tags:
            return [""]
        candidates = list(tags)
        random.shuffle(candidates)
        return candidates

    async def _claim_checkin_background_usage(
        self, event: AstrMessageEvent, source_key: str, illust_id: str
    ) -> bool:
        if self.image_index is None or not source_key or not illust_id:
            return True
        try:
            claimed = await self.image_index.claim_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature="checkin_pending",
                user_id=str(event.get_sender_id() or ""),
            )
            logger.debug(
                f"{LOG_PREFIX} 签到背景占用结果: "
                f"result={'claimed' if claimed else 'duplicate'} "
                f"source={source_key.partition(':')[0] or 'unknown'} "
                f"illust_id={illust_id}"
            )
            return claimed
        except Exception as exc:
            logger.debug(
                f"{LOG_PREFIX} 签到背景占用失败，拒绝使用候选: "
                f"reason=index_error error_type={type(exc).__name__}"
            )
            return False

    async def _release_checkin_background_usage(
        self, event: AstrMessageEvent, source_key: str, illust_id: str
    ) -> None:
        if self.image_index is None or not source_key or not illust_id:
            return
        try:
            await self.image_index.release_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature="checkin_pending",
            )
            logger.debug(
                f"{LOG_PREFIX} 签到背景占用已释放: "
                f"source={source_key.partition(':')[0] or 'unknown'} "
                f"illust_id={illust_id}"
            )
        except Exception as exc:
            logger.debug(
                f"{LOG_PREFIX} 签到背景占用释放未完成: "
                f"reason=index_error error_type={type(exc).__name__}"
            )

    async def _release_checkin_background_claim(
        self, event: AstrMessageEvent, background: CardBackground | None
    ) -> None:
        if (
            background is None
            or background.mode != "pixiv_daily"
            or not background.source
            or not background.illust_id
        ):
            return
        await self._release_checkin_background_usage(
            event, background.source, background.illust_id
        )

    async def _checkin_background_used_ids(
        self, event: AstrMessageEvent, source_key: str
    ) -> set[str]:
        used_ids: set[str] = set()
        if self.image_index is not None:
            try:
                used_ids.update(
                    await self.image_index.get_used_illust_ids(
                        self._event_scope(event), source_key
                    )
                )
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景读取去重索引失败: "
                    f"error_type={type(e).__name__}"
                )
        return used_ids

    def _checkin_avatar_url(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return ""
        platform = event.get_platform_name()
        if platform == "aiocqhttp" and user_id.isdigit():
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return ""
