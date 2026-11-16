from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import random
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.star_tools import StarTools

from ..pixiv.client import SourceRequest
from .models import CheckinRecord
from .background import (
    CHECKIN_ARTWORK_ASPECT_PARAM,
    CHECKIN_ARTWORK_TARGET_RATIO,
    CHECKIN_ARTWORK_TOLERANCE,
    filter_illusts_by_aspect_ratio,
)
from .card import (
    CHECKIN_CARD_HEIGHT,
    CHECKIN_CARD_WIDTH,
    CardBackground,
    BackgroundEncodingError,
    _file_to_data_url,
    build_checkin_card_data,
    get_checkin_card_template,
)
from .cache import is_valid_card_jpeg
from .calendar import (
    CHECKIN_CALENDAR_HEIGHT,
    CHECKIN_CALENDAR_WIDTH,
    build_checkin_calendar_data,
    get_checkin_calendar_template,
)
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

# 日历极致档输出 2880×1620，背景按同一上限压缩，避免先缩到卡片尺寸再放大。
_CALENDAR_ARTWORK_MAX_EDGE = 2880


def _calendar_background_data_url(path: str) -> str:
    return _file_to_data_url(path, max_edge=_CALENDAR_ARTWORK_MAX_EDGE)


def _checkin_background_mode_label(background: CardBackground | None) -> str:
    mode = getattr(background, "mode", "") or "none"
    return _CHECKIN_BACKGROUND_MODE_LABELS.get(str(mode), str(mode))

_CALENDAR_BG_RATIO = 16 / 9
_CALENDAR_BG_RATIO_TOLERANCE = 0.10
# Lolicon aspectRatio 支持数值条件组合（gt/gte/lt/lte/eq）；
# "landscape"/"portrait" 等枚举值不是合法语法，传了等于没筛。
_CALENDAR_BG_ASPECT_PARAM = "gt1.7lt1.8"


try:
    from ..pixiv.index import ordered_by_unused
    from ..pixiv.usage import usage_id
    from ..pixiv.downloader import cleanup
except ImportError:  # Direct imports used by the test suite.
    from pixiv.index import ordered_by_unused
    from pixiv.usage import usage_id
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
        *,
        policy=None,
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

        if policy is None:
            policy = await self._content_safety_policy(event)
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
                illust, record.background_illust_id, policy
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
                size_limit_bytes=0,
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

    async def _prepare_checkin_card_data(
        self,
        event: AstrMessageEvent,
        *,
        profile,
        record,
        background,
        bot_name,
        user_title: str = "",
    ) -> dict[str, object]:
        """下载并内联单次渲染资源，供画质降级复用。"""
        avatar_url = ""
        source = (
            self._checkin_avatar_url(event)
            if self._cfg_bool("checkin_avatar_enabled", True)
            else ""
        )
        if source:
            path = ""
            try:
                path, _size = await self.downloader.download(
                    source, timeout=self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
                )
                avatar_url = await asyncio.to_thread(_file_to_data_url, path)
                if not avatar_url:
                    raise ValueError("invalid avatar image")
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到头像获取失败，使用昵称占位: "
                    f"error_type={type(exc).__name__}"
                )
            finally:
                cleanup(path)
        return await asyncio.to_thread(
            build_checkin_card_data,
            profile=profile,
            record=record,
            bot_name=bot_name,
            avatar_url=avatar_url,
            background=background,
            user_title=user_title,
            background_refresh_cost=self._cfg_int(
                "checkin_background_refresh_cost", 100, 0, 500
            ),
        )

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
        prepared_data: dict[str, object] | None = None,
    ) -> str:
        width = CHECKIN_CARD_WIDTH
        height = CHECKIN_CARD_HEIGHT
        render_spec = get_checkin_render_tier(
            render_tier or self._configured_checkin_render_tier()
        )
        data = (
            prepared_data
            if prepared_data is not None
            else await self._prepare_checkin_card_data(
                event,
                profile=profile,
                record=record,
                bot_name=bot_name,
                background=background,
                user_title=user_title,
            )
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
        policy,
        safety_identity=None,
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
                policy=policy,
                safety_identity=safety_identity,
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
        policy=None,
        safety_identity=None,
    ) -> tuple[Path, str]:
        """复用资源逐档渲染；源背景编码失败交由调用方撤销作品选择。"""
        last_error: Exception | None = None
        fallback_specs = checkin_render_fallbacks(preferred_tier)
        prepared_data = None
        for tier_index, spec in enumerate(fallback_specs):
            renderer_source_path = ""
            render_succeeded = False
            started_at = time.monotonic()

            async def render_card() -> str:
                nonlocal renderer_source_path, prepared_data
                if prepared_data is None:
                    prepared_data = await self._prepare_checkin_card_data(
                        event,
                        profile=profile,
                        record=record,
                        background=background,
                        bot_name=bot_name,
                        user_title=user_title,
                    )
                renderer_source_path = await self._render_checkin_card(
                    event,
                    profile=profile,
                    record=record,
                    background=background,
                    bot_name=bot_name,
                    user_title=user_title,
                    render_tier=spec.name,
                    prepared_data=prepared_data,
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
                        policy=policy,
                        safety_identity=safety_identity,
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
            except BackgroundEncodingError:
                # 降画质不能修复被拒绝的源图，交回业务层撤销作品选择。
                raise
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
        policy=None,
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
            logger.info(f"{LOG_PREFIX} 签到自定义背景不可用，回退在线图片源背景")
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
            _policy=policy,
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
        _policy=None,
    ) -> CardBackground | None:
        if _policy is None:
            _policy = await self._content_safety_policy(event)
        if _selected_tag is None:
            tag_config = self._cfg_str("checkin_background_tag", "")
            for selected_tag in self._checkin_background_tag_candidates(tag_config):
                if not selected_tag and tag_config.strip():
                    logger.info(f"{LOG_PREFIX} 签到标签未取得背景，尝试随机推荐: fallback=untagged")
                background = await self._download_checkin_pixiv_background(
                    event,
                    record,
                    claim_usage=claim_usage,
                    preview_nonce=preview_nonce,
                    preview_excluded_ids=preview_excluded_ids,
                    background_quality=background_quality,
                    _selected_tag=selected_tag,
                    _policy=_policy,
                )
                if background is not None:
                    return background
            return None

        selected_tag = _selected_tag

        source_key = ""
        used_ids: set[str] = {usage_id(value) for value in preview_excluded_ids or ()}
        used_source_key = ""
        illusts: list[dict] = []
        raw_count = 0
        request_state = SourceRequest()
        page_attempt = 0
        while page_attempt < CHECKIN_BACKGROUND_PAGE_ATTEMPTS:
            retried_home = request_state.retried_first_page
            try:
                illusts, raw_count, source_key = await self._fetch_source_candidates(
                    event, selected_tag, count=20,
                    aspect_ratio=CHECKIN_ARTWORK_ASPECT_PARAM,
                    use_page_cursor=not preview_nonce, feature="checkin",
                    policy=_policy, request_state=request_state,
                )
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景请求失败: tag_configured={'yes' if selected_tag else 'no'} "
                    f"error_type={type(exc).__name__}"
                )
                return None
            page_attempt += 1 + int(request_state.retried_first_page and not retried_home)
            if source_key != used_source_key:
                used_ids = {usage_id(value) for value in preview_excluded_ids or ()}
                used_ids.update(await self._checkin_background_used_ids(event, source_key))
                used_source_key = source_key
            if self._cfg_bool("filter_manga", True):
                illusts = self._filter_manga(illusts)
            try:
                illusts = await self._filter_blacklisted_illusts(illusts, _policy)
            except RuntimeError as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景安全检查不可用，本阶段停止选图: error_type={type(exc).__name__}"
                )
                return None
            illusts = filter_illusts_by_aspect_ratio(
                illusts, CHECKIN_ARTWORK_TARGET_RATIO, CHECKIN_ARTWORK_TOLERANCE,
            )
            reason = "empty_batch" if raw_count == 0 else "filtered_out" if not illusts else "used_or_pending"
            fresh = [
                illust for illust in ordered_by_unused(illusts, used_ids)
                if usage_id(illust.get("id")) not in used_ids
            ]
            if fresh:
                illusts = fresh
                break
            if not preview_nonce and self.image_index is not None and request_state.source == "pixiv":
                await self._save_checkin_page_cursor(event, source_key, request_state)
            logger.info(
                f"{LOG_PREFIX} 签到背景本批未选出可用作品: batch={page_attempt} raw_count={raw_count} "
                f"reason={reason} has_next={'no' if request_state.exhausted else 'yes'} "
                f"selection={'tagged' if selected_tag else 'untagged'}"
            )
            if request_state.exhausted:
                return None
        else:
            logger.info(
                f"{LOG_PREFIX} 签到背景本阶段候选批次已耗尽: max_batches={CHECKIN_BACKGROUND_PAGE_ATTEMPTS} "
                f"selection={'tagged' if selected_tag else 'untagged'}"
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
                reason = await self._blacklist_reason_for_illust(
                    illust, illust_id, _policy
                )
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
                        f"{LOG_PREFIX} 签到背景候选未取得占用: illust_id={illust_id} reason=claim_rejected"
                    )
                    continue
            title = illust.get("title", "无标题")
            background_ready = False
            try:
                path, actual_q, file_size = await self.downloader.download_for_send(
                    illust,
                    background_quality,
                    timeout=timeout_sec,
                    size_limit_bytes=0,
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
                logger.info(
                    f"{LOG_PREFIX} 签到背景选择完成: mode=pixiv_daily "
                    f"source={source_key.partition(':')[0] or 'unknown'} "
                    f"selection={'tagged' if selected_tag else 'untagged'} "
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
        if not preview_nonce and len(ordered) <= 8 and request_state.source == "pixiv":
            await self._save_checkin_page_cursor(event, source_key, request_state)
        logger.info(
            f"{LOG_PREFIX} 签到背景本阶段未取得可下载作品: "
            f"selection={'tagged' if selected_tag else 'untagged'} candidate_count={min(len(ordered), 8)}"
        )
        return None

    async def _save_checkin_page_cursor(self, event, source_key, state):
        if self.image_index is not None:
            try:
                await self.image_index.save_page_cursor(
                    self._event_scope(event), source_key, state.next_params, feature="checkin"
                )
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 签到背景续页位置保存失败: error_type={type(exc).__name__}")

    def _checkin_background_tag_candidates(self, tag_config: object) -> list[str]:
        tags = self._split_config_tags(tag_config)
        if not tags:
            return [""]
        candidates = list(tags)
        random.shuffle(candidates)
        return candidates + [""]

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

    async def _prepare_checkin_calendar_background(
        self,
        event: AstrMessageEvent,
        *,
        user_id: str,
        background_quality: str = "medium",
        policy=None,
    ) -> CardBackground | None:
        """为日历取一张 16:9 邻域横图氛围背景；不占去重池，失败按无图兜底。

        ``background_quality`` 跟随签到卡画质档位（省流量=medium、清晰/极致=large）；
        两源在下载器内已归一化到同一质量枚举，无需按来源区分。
        """
        if policy is None:
            policy = await self._content_safety_policy(event)
        try:
            illusts, _raw_count, source_key = await self._fetch_source_candidates(
                event,
                "",
                count=20,
                aspect_ratio=_CALENDAR_BG_ASPECT_PARAM,
                use_page_cursor=False,
                policy=policy,
            )
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 日历背景请求失败，使用无图兜底: "
                f"error_type={type(exc).__name__}"
            )
            return CardBackground(mode="fallback", source="fallback")
        if self._cfg_bool("filter_manga", True):
            illusts = self._filter_manga(illusts)
        try:
            illusts = await self._filter_blacklisted_illusts(illusts, policy)
        except RuntimeError as exc:
            logger.warning(
                f"{LOG_PREFIX} 日历背景安全检查不可用，使用无图兜底: "
                f"error_type={type(exc).__name__}"
            )
            return CardBackground(mode="fallback", source="fallback")
        # Pixiv 路径不经过 API 端筛选，本地按 16:9±0.1 复核。
        illusts = filter_illusts_by_aspect_ratio(
            illusts, _CALENDAR_BG_RATIO, _CALENDAR_BG_RATIO_TOLERANCE
        )
        used_ids = set(await self._checkin_background_used_ids(event, source_key))
        ordered = [
            illust
            for illust in ordered_by_unused(illusts, used_ids)
            if str(illust.get("id") or "") not in used_ids
        ] or illusts
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        for illust in ordered[:8]:
            illust_id = str(illust.get("id") or "")
            if not illust_id:
                continue
            try:
                reason = await self._blacklist_reason_for_illust(
                    illust, illust_id, policy
                )
            except RuntimeError as exc:
                logger.warning(
                    f"{LOG_PREFIX} 日历背景安全检查不可用，跳过作品: "
                    f"illust_id={illust_id} error_type={type(exc).__name__}"
                )
                continue
            if reason:
                continue
            try:
                path, actual_q, file_size = await self.downloader.download_for_send(
                    illust,
                    background_quality,
                    timeout=timeout_sec,
                    size_limit_bytes=0,
                    log_context=f"[日历背景] 作品 {illust_id}",
                )
            except Exception as exc:
                logger.debug(
                    f"{LOG_PREFIX} 日历背景候选跳过: reason=download_error "
                    f"illust_id={illust_id} error_type={type(exc).__name__}"
                )
                continue
            return CardBackground(
                image_path=path,
                mode="pixiv_daily",
                source=source_key,
                illust_id=illust_id,
                title=str(illust.get("title") or ""),
                author=str((illust.get("user") or {}).get("name") or ""),
                illust=illust,
                quality=actual_q,
                file_size=file_size,
            )
        logger.info(
            f"{LOG_PREFIX} 日历背景无可用横图，使用无图兜底: "
            f"reason=no_landscape_candidate"
        )
        return CardBackground(mode="fallback", source="fallback")

    async def _render_checkin_calendar(
        self,
        *,
        month: str,
        today_key: str,
        records,
        background: CardBackground | None = None,
        events=(),
        render_tier: str | None = None,
    ) -> str:
        background_credit = ""
        if background is not None and getattr(background, "illust_id", ""):
            background_credit = getattr(background, "pixiv_caption", "") or ""
        # 读文件 + PIL 校验 + base64 可达数 MB，与视图组装一并放进线程池
        background_url = await asyncio.to_thread(
            _calendar_background_data_url,
            str(getattr(background, "image_path", "") or ""),
        )
        data = await asyncio.to_thread(
            build_checkin_calendar_data,
            month=month,
            today_key=today_key,
            records=records,
            background_url=background_url,
            background_credit=background_credit,
            events=events,
        )
        # 与签到卡同一缩放语义：clip/viewport 恒为设计画布 1600×900，
        # 输出分辨率由端点 device_scale_factor 决定（省流量无缩放、清晰 1.3x、极致 1.8x）
        render_spec = get_checkin_render_tier(
            render_tier or DEFAULT_CHECKIN_RENDER_TIER
        )
        options = {
            "full_page": True,
            "type": "jpeg",
            "quality": CHECKIN_JPEG_QUALITY,
            "clip": {
                "x": 0,
                "y": 0,
                "width": CHECKIN_CALENDAR_WIDTH,
                "height": CHECKIN_CALENDAR_HEIGHT,
            },
            "viewport": {
                "width": CHECKIN_CALENDAR_WIDTH,
                "height": CHECKIN_CALENDAR_HEIGHT,
            },
            "animations": "disabled",
        }
        if render_spec.scale_level is not None:
            options["device_scale_factor_level"] = render_spec.scale_level
        return await self.html_render(
            get_checkin_calendar_template(),
            data,
            return_url=False,
            options=options,
        )

    async def _checkin_background_used_ids(
        self, event: AstrMessageEvent, source_key: str
    ) -> set[str]:
        used_ids: set[str] = set()
        if self.image_index is not None:
            try:
                used_ids.update(
                    await self.image_index.get_used_illust_ids(
                        self._event_scope(event), source_key, feature="checkin"
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
