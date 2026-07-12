from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path

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
    CHECKIN_CARD_TEMPLATE,
    CHECKIN_CARD_WIDTH,
    CardBackground,
    build_checkin_card_data,
)
try:
    from ..pixiv.index import ordered_by_unused
except ImportError:  # Direct imports used by the test suite.
    from pixiv.index import ordered_by_unused


LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
CHECKIN_BACKGROUND_PAGE_ATTEMPTS = 5
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0
RANKING_MODES = {
    "day": "今日",
    "week": "本周",
    "month": "本月",
    "day_male": "男性向",
    "day_female": "女性向",
    "week_original": "原创",
    "week_rookie": "新人",
    "day_manga": "漫画",
}


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
        if background.image_path and background.illust:
            await self._record_sent_image(
                event,
                illust,
                background.image_path,
                source="checkin",
                quality=background.quality,
                file_size=background.file_size,
            )
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
                return replace(saved, image_path=str(custom_path), mode="custom")
            return replace(saved, mode="fallback")

        if not record.background_illust_id:
            return replace(saved, mode="fallback")
        if self.client is None:
            self._init_client()
        if self.client is None:
            return replace(saved, mode="fallback")

        try:
            illust = await self.client.illust_detail(int(record.background_illust_id))
            if not illust:
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

            quality = self._cfg_str("image_quality", "original")
            timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
            downgrade_limit_mb = self._cfg_float(
                "auto_downgrade_original_mb",
                DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
                0.0,
                100.0,
            )
            path, actual_quality, file_size = await self.downloader.download_for_send(
                illust,
                quality,
                proxy=self._cfg_str("pixiv_proxy_url"),
                timeout=timeout_sec,
                downgrade_limit_bytes=int(downgrade_limit_mb * 1024 * 1024),
                log_context=f"[签到背景恢复] 作品 {record.background_illust_id}",
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
                f"{LOG_PREFIX} 签到背景 {record.background_illust_id} 恢复失败，使用占位图: {e}"
            )
            return replace(saved, mode="fallback")



    async def _render_checkin_card(
        self,
        event: AstrMessageEvent,
        *,
        profile,
        record,
        background: CardBackground | None,
        bot_name: str,
        user_title: str = "",
    ) -> str:
        avatar_url = self._checkin_avatar_url(event) if self._cfg_bool("checkin_avatar_enabled", True) else ""
        width = CHECKIN_CARD_WIDTH
        height = CHECKIN_CARD_HEIGHT
        quality = self._cfg_int("checkin_card_quality", 95, 60, 100)
        data = build_checkin_card_data(
            profile=profile,
            record=record,
            bot_name=bot_name,
            avatar_url=avatar_url,
            background=background,
            user_title=user_title,
            width=width,
            height=height,
        )
        return await self.html_render(
            CHECKIN_CARD_TEMPLATE,
            data,
            return_url=False,
            options={
                "full_page": False,
                "type": "jpeg",
                "quality": quality,
                "clip": {"x": 0, "y": 0, "width": width, "height": height},
                "viewport": {"width": width, "height": height},
                "animations": "disabled",
            },
        )



    async def _prepare_checkin_background(
        self, event: AstrMessageEvent, record, *, claim_usage: bool = True
    ) -> CardBackground | None:
        mode = self._cfg_str("checkin_background_mode", "pixiv_daily") or "pixiv_daily"
        if mode == "custom":
            custom_path = self._resolve_custom_background_path(
                self._cfg_str("checkin_custom_background", "")
            )
            if custom_path:
                return CardBackground(
                    image_path=str(custom_path),
                    mode="custom",
                    source="custom",
                )
            logger.warning(f"{LOG_PREFIX} 签到自定义背景不可用，回退 Pixiv 背景")
        elif mode != "pixiv_daily":
            mode = "pixiv_daily"
        pixiv_bg = await self._download_checkin_pixiv_background(
            event, record, claim_usage=claim_usage
        )
        if pixiv_bg is not None:
            return pixiv_bg
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
        except OSError:
            return None
        if not resolved.is_file():
            return None
        if resolved.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            return None
        try:
            from PIL import Image as PILImage
        except ImportError:
            logger.warning(f"{LOG_PREFIX} 未安装 Pillow，跳过背景完整性校验")
            return resolved
        try:
            with PILImage.open(resolved) as img:
                img.verify()
        except Exception:
            logger.warning(
                f"{LOG_PREFIX} 签到自定义背景文件无效或损坏: {resolved}"
            )
            return None
        return resolved



    async def _download_checkin_pixiv_background(
        self, event: AstrMessageEvent, record, *, claim_usage: bool = True
    ) -> CardBackground | None:
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.info(f"{LOG_PREFIX} 签到背景跳过 Pixiv：未配置 refresh_token")
            return None
        if self.client is None:
            self._init_client()
        if self.client is None:
            return None

        tag_config = self._cfg_str("checkin_background_tag", "")
        tags = self._split_config_tags(tag_config)
        selected_tag = ""
        if tags:
            seed = int.from_bytes(
                hashlib.sha256(
                    f"checkin-bg-tag|{record.date_key}|{tag_config}".encode("utf-8")
                ).digest()[:8],
                "big",
            )
            selected_tag = tags[seed % len(tags)]
        ranking_mode = self._cfg_str("pixiv_ranking_mode", "week")
        if ranking_mode not in RANKING_MODES:
            ranking_mode = "week"

        source_key = self._source_key(selected_tag, ranking_mode)
        used_ids = await self._checkin_background_used_ids(event, source_key)
        illusts: list[dict] = []
        raw_count = 0

        for page_attempt in range(1, CHECKIN_BACKGROUND_PAGE_ATTEMPTS + 1):
            # 获取作品列表（带分页游标）
            illusts, raw_count, source_key = await self._fetch_paginated(
                event, selected_tag, ranking_mode
            )
            if not illusts:
                return None

            r18_mode = self._cfg_int("pixiv_r18", 0, 0, 2)
            illusts = self._filter_r18(illusts, r18_mode)
            is_manga_ranking = not selected_tag and ranking_mode == "day_manga"
            if self._cfg_bool("filter_manga", True) and not is_manga_ranking:
                illusts = self._filter_manga(illusts)
            illusts = await self._filter_blacklisted_illusts(illusts)
            illusts = filter_illusts_by_aspect_ratio(
                illusts,
                CHECKIN_ARTWORK_TARGET_RATIO,
                CHECKIN_ARTWORK_TOLERANCE,
            )
            if not illusts:
                if self.image_index is None:
                    return None
                try:
                    await self.image_index.advance_page_offset(
                        self._event_scope(event), source_key, raw_count
                    )
                    logger.info(
                        f"{LOG_PREFIX} 签到背景第 {page_attempt} 页无符合 3:4 的竖向作品，切换下一页"
                    )
                except Exception as e:
                    logger.warning(f"{LOG_PREFIX} 签到背景分页游标更新失败: {e}")
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
            if self.image_index is None:
                logger.info(f"{LOG_PREFIX} 签到背景候选均已在图片历史中，跳过 Pixiv 背景")
                return None

            try:
                await self.image_index.advance_page_offset(
                    self._event_scope(event), source_key, raw_count
                )
                logger.info(
                    f"{LOG_PREFIX} 签到背景第 {page_attempt} 页候选均已使用，切换下一页"
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 签到背景分页游标更新失败: {e}")
                return None
        else:
            logger.info(
                f"{LOG_PREFIX} 签到背景连续 {CHECKIN_BACKGROUND_PAGE_ATTEMPTS} 页无可用竖向作品"
            )
            return None

        seed = int.from_bytes(
            hashlib.sha256(
                f"checkin-bg|{record.date_key}|{record.user_id}|{source_key}".encode(
                    "utf-8"
                )
            ).digest()[:8],
            "big",
        )
        start = seed % len(illusts)
        ordered = illusts[start:] + illusts[:start]
        quality = self._cfg_str("image_quality", "original")
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            100.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)
        for idx, illust in enumerate(ordered[:8], 1):
            illust_id = str(illust.get("id") or "")
            if not illust_id:
                continue
            reason = await self._blacklist_reason_for_illust(illust, illust_id)
            if reason:
                logger.info(f"{LOG_PREFIX} 签到背景跳过：{reason}")
                continue
            claimed = False
            if claim_usage:
                claimed = await self._claim_checkin_background_usage(
                    event, source_key, illust_id
                )
                if not claimed:
                    logger.info(
                        f"{LOG_PREFIX} 签到背景跳过：作品 {illust_id} 已被其他签到占用"
                    )
                    continue
            title = illust.get("title", "无标题")
            try:
                path, actual_q, file_size = await self.downloader.download_for_send(
                    illust,
                    quality,
                    proxy=self._cfg_str("pixiv_proxy_url"),
                    timeout=timeout_sec,
                    downgrade_limit_bytes=downgrade_limit_bytes,
                    log_context=f"[签到背景 {idx}] 作品 {illust_id} 「{title}」",
                )
                author = str((illust.get("user") or {}).get("name") or "")
                return CardBackground(
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
            except asyncio.TimeoutError:
                logger.warning(
                    f"{LOG_PREFIX} 签到背景 {illust_id} 下载超时 ({timeout_sec}s)"
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 签到背景 {illust_id} 下载失败: {e}")
            if claimed:
                await self._release_checkin_background_usage(
                    event, source_key, illust_id
                )
        return None



    async def _claim_checkin_background_usage(
        self, event: AstrMessageEvent, source_key: str, illust_id: str
    ) -> bool:
        if self.image_index is None or not source_key or not illust_id:
            return True
        try:
            return await self.image_index.claim_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature="checkin_pending",
                user_id=str(event.get_sender_id() or ""),
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 签到背景占用索引失败: {e}")
            return True



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
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 释放签到背景占用失败: {e}")



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
                logger.warning(f"{LOG_PREFIX} 签到背景读取去重索引失败: {e}")
        if self.image_history is not None:
            try:
                records = await self.image_history.list_records()
                used_ids.update(
                    str(record.get("illust_id") or "").strip()
                    for record in records
                    if str(record.get("illust_id") or "").strip()
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 签到背景读取图片历史失败: {e}")
        return used_ids



    def _checkin_avatar_url(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return ""
        platform = event.get_platform_name()
        if platform == "aiocqhttp" and user_id.isdigit():
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        return ""
