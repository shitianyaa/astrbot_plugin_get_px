from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

from astrbot.api import logger
from astrbot.api.all import Image, Plain
from astrbot.api.event import AstrMessageEvent

from .models import ACHIEVEMENTS, CheckinProfile, CheckinRecord
from .quality import CHECKIN_JPEG_QUALITY, get_checkin_render_tier
from .birthday import birthday_matches, parse_qq_birthday
from .card import BackgroundEncodingError, CardBackground, build_checkin_card_data
from .content import CheckinContent, resolve_checkin_content
from .greeting import DEFAULT_CHECKIN_GREETING_PROMPT
from .themes import get_checkin_theme

try:
    from ..pixiv.delivery import DeliveryNotSubmitted, DeliveryStatus, send_once
    from ..pixiv.downloader import cleanup
except ImportError:  # Direct imports used by the test suite.
    from pixiv.delivery import DeliveryNotSubmitted, DeliveryStatus, send_once
    from pixiv.downloader import cleanup


LOG_PREFIX = "[GetPx]"


@dataclass(frozen=True)
class QQBirthdayLookup:
    value: tuple[int, int] | None
    definitive: bool


class CheckinApplicationMixin:
    """Run the daily check-in flow and persist its content snapshot."""

    @staticmethod
    def _event_username(event: AstrMessageEvent, default: str) -> str:
        try:
            value = event.get_sender_name()
        except Exception:
            value = None
        if value:
            return str(value)

        message_obj = getattr(event, "message_obj", None)
        sender = getattr(message_obj, "sender", None)
        for attr in ("nickname", "name", "user_name"):
            value = getattr(sender, attr, None)
            if value:
                return str(value)
        return default

    @staticmethod
    def _event_group_context(event: AstrMessageEvent) -> tuple[str, str, str]:
        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:
            group_id = ""
        if not group_id:
            return "", "", ""
        message_obj = getattr(event, "message_obj", None)
        group_name = ""
        for source in (message_obj, getattr(message_obj, "raw_message", None)):
            if isinstance(source, dict):
                value = source.get("group_name") or source.get("groupName")
            else:
                value = getattr(source, "group_name", None)
            if value:
                group_name = str(value).strip()
                break
        try:
            platform = str(event.get_platform_name() or "").strip()
        except Exception:
            platform = ""
        return group_id, group_name or group_id, platform

    def _checkin_background_claims_enabled(self) -> bool:
        image_index = getattr(self, "image_index", None)
        if image_index is None:
            return False
        try:
            return int(getattr(image_index, "retention_days", 1)) > 0
        except (TypeError, ValueError):
            # 未知的第三方索引实现按启用处理，避免误释放真实占用。
            return True

    async def _handle_checkin(
        self,
        event: AstrMessageEvent,
        *,
        silent_when_disabled: bool = False,
        _flow_locked: bool = False,
    ):
        """Create or resend today's persisted check-in card."""
        if not self._cfg_bool("checkin_enabled", True):
            if not silent_when_disabled:
                yield event.plain_result("签到功能已关闭")
            return
        if self.checkin_store is None:
            yield event.plain_result("签到数据尚未初始化，请稍后再试")
            return

        user_id = str(event.get_sender_id() or "")
        if not user_id:
            yield event.plain_result("无法识别用户 ID，暂时不能签到")
            return
        username = self._event_username(event, user_id)
        bot_name = self._cfg_str("checkin_bot_name", "neko") or "neko"
        group_id, group_name, platform = self._event_group_context(event)

        if not _flow_locked:
            lock_key = user_id
            lock = self._checkin_flow_lock(lock_key)
            async with lock:
                outputs = [
                    item
                    async for item in self._handle_checkin(
                        event,
                        silent_when_disabled=silent_when_disabled,
                        _flow_locked=True,
                    )
                ]
            for output in outputs:
                yield output
            return

        try:
            theme = get_checkin_theme("default")
            get_preference = getattr(self.checkin_store, "get_user_preference", None)
            if callable(get_preference):
                preference = await get_preference(user_id)
                theme = get_checkin_theme(
                    getattr(preference, "current_theme_id", "default")
                )
            result = await self.checkin_store.checkin(
                user_id=user_id,
                username=username,
                bot_name=bot_name,
                theme_id=theme.theme_id,
                template_version=theme.template_version,
                group_id=group_id,
                group_name=group_name,
                platform=platform,
            )
        except Exception as e:
            logger.error(
                f"{LOG_PREFIX} 签到写入失败: stage=checkin "
                f"error_type={type(e).__name__}"
            )
            yield event.plain_result("签到失败，请稍后再试")
            return

        record = result.record
        if record is None:
            yield event.plain_result(self._format_checkin_plain_text(result))
            return

        try:
            record = await self._prepare_checkin_record_content(
                event,
                record,
                allow_ai=not result.duplicate,
            )
            result = replace(result, record=record)
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX} 签到内容持久化失败，回退纯文字: "
                f"stage=local_content error_type={type(e).__name__}"
            )
            yield event.plain_result(self._format_checkin_plain_text(result))
            return

        cache = getattr(self, "checkin_cache", None)
        if cache is None:
            logger.warning(
                f"{LOG_PREFIX} 签到卡回退纯文字: reason=cache_unavailable"
            )
            yield event.plain_result(self._format_checkin_plain_text(result))
            return

        background: CardBackground | None = None
        card_path: Path | None = None
        cached_path: Path | None = None
        claim_held = False
        background_persisted = False
        profile_snapshot = self._checkin_profile_from_record(record)
        user_title = await self._get_checkin_user_title(record.user_id)
        preferred_tier = (
            self._record_checkin_render_tier(record)
            if result.duplicate
            else self._configured_checkin_render_tier()
        )
        stage = "background_selection"
        cache_hit = False
        try:
            policy = await self._content_safety_policy(event)
            safety_identity = policy.cache_identity()
            if result.duplicate:
                background = self._checkin_background_from_record(record)
                variant_key = self._checkin_card_cache_key(
                    event, profile=profile_snapshot, record=record,
                    background=background, bot_name=bot_name, user_title=user_title,
                    render_tier=preferred_tier, safety_identity=safety_identity,
                )
                background = (
                    await asyncio.to_thread(cache.get_background, record.date_key, variant_key)
                    or background
                )
            else:
                background = await self._prepare_checkin_background(
                    event,
                    record,
                    render_tier=preferred_tier,
                    policy=policy,
                )
                claim_held = bool(
                    background is not None
                    and background.mode == "pixiv_daily"
                    and background.illust_id
                    and self._checkin_background_claims_enabled()
                )

            stage = "cache_lookup"
            cached_path, actual_tier = await self._get_cached_checkin_card(
                event,
                cache=cache,
                profile=profile_snapshot,
                record=record,
                background=background,
                bot_name=bot_name,
                user_title=user_title,
                preferred_tier=preferred_tier,
                policy=policy,
                safety_identity=safety_identity,
            )
            cache_hit = cached_path is not None
            if cached_path is None and result.duplicate:
                stage = "background_restore"
                background = await self._restore_checkin_background(
                    event, record, policy=policy
                )
                if background.mode != "pixiv_daily" and (
                    record.background_mode == "pixiv_daily"
                ):
                    # 在线背景恢复失败（作品被删、换图、不符规范等）时重新选一张，
                    # 而不是直接发内置占位图；custom 背景恢复失败保持占位行为。
                    stage = "background_reselect"
                    logger.info(
                        f"{LOG_PREFIX} 签到背景恢复失败，重新选择在线背景: "
                        f"illust_id={record.background_illust_id} "
                        f"restored_mode={background.mode}"
                    )
                    reselected = await self._prepare_checkin_background(
                        event,
                        record,
                        render_tier=preferred_tier,
                        policy=policy,
                    )
                    if reselected is not None and reselected.mode == "pixiv_daily":
                        background = reselected
                        # mode 已由重选分支保证为 pixiv_daily，仅查占用与 illust_id。
                        claim_held = bool(
                            background.illust_id
                            and self._checkin_background_claims_enabled()
                        )
                        logger.info(
                            f"{LOG_PREFIX} 签到背景重选完成: "
                            f"illust_id={background.illust_id} "
                            f"source={background.source.partition(':')[0] or 'unknown'} "
                            f"quality={background.quality}"
                        )
                    else:
                        logger.debug(
                            f"{LOG_PREFIX} 签到背景重选失败，使用占位图: "
                            f"reselected_mode={getattr(reselected, 'mode', 'none')}"
                        )
            if cached_path is None:
                stage = "card_render"
                for background_attempt in range(2):
                    try:
                        cached_path, actual_tier = await self._render_checkin_card_with_fallback(
                            event,
                            profile=profile_snapshot,
                            record=record,
                            background=background,
                            bot_name=bot_name,
                            user_title=user_title,
                            preferred_tier=preferred_tier,
                            cache=cache,
                            policy=policy,
                            safety_identity=safety_identity,
                        )
                        break
                    except BackgroundEncodingError:
                        if background_attempt:
                            raise
                        logger.warning(
                            f"{LOG_PREFIX} 签到背景无法编码，撤销作品并使用占位图: "
                            f"reason=background_encoding_failed"
                        )
                        if claim_held:
                            await self._release_checkin_background_claim(event, background)
                            claim_held = False
                        if background and background.mode == "pixiv_daily":
                            cleanup(background.image_path)
                        background = CardBackground(mode="fallback", source="fallback")

            if not result.duplicate and background is not None:
                stage = "background_persist"
                await self.checkin_store.update_record_background(
                    user_id=user_id,
                    date_key=record.date_key,
                    mode=background.mode,
                    source=background.source,
                    illust_id=background.illust_id,
                    title=background.title,
                    author=background.author,
                    quality=background.quality,
                )
                background_persisted = True
                record = replace(
                    record,
                    background_mode=background.mode,
                    background_source=background.source,
                    background_illust_id=background.illust_id,
                    background_title=background.title,
                    background_author=background.author,
                    background_quality=background.quality,
                )
            if not result.duplicate:
                stage = "render_tier_persist"
                record = await self._persist_checkin_render_tier(record, actual_tier)
            result = replace(result, record=record)
            card_path = cached_path
            if card_path:
                content = [Image.fromFileSystem(str(card_path))]
                if background and background.pixiv_caption:
                    content.append(Plain(background.pixiv_caption))
                if not result.duplicate:
                    quota_line = await self._grant_checkin_omnidraw_quota(
                        event, user_id
                    )
                    if quota_line:
                        content.append(Plain(quota_line))
                stage = "card_send"
                submission_cancelled = None
                try:
                    delivery_status = await send_once(event, content, context="feature=checkin")
                except DeliveryNotSubmitted:
                    raise
                except asyncio.CancelledError as exc:
                    # 保留派生背景摘要后再传播取消，重选卡片也可按原背景复用。
                    submission_cancelled = exc
                    delivery_status = DeliveryStatus.UNKNOWN
                if delivery_status is DeliveryStatus.FAILED:
                    raise RuntimeError("签到卡尚未提交")
                # 正常返回、超时或送达未知均不能撤销已提交背景。
                claim_held = False
                if not cache_hit:
                    try:
                        # 用当天基础记录定位变体；跨群重选不覆盖基础背景。
                        variant_key = self._checkin_card_cache_key(
                            event, profile=profile_snapshot, record=record,
                            background=self._checkin_background_from_record(record),
                            bot_name=bot_name, user_title=user_title,
                            render_tier=self._record_checkin_render_tier(record),
                            safety_identity=safety_identity,
                        )
                        await asyncio.to_thread(
                            cache.store_background, record.date_key, variant_key, background
                        )
                    except Exception as exc:
                        logger.warning(
                            f"{LOG_PREFIX} 签到背景缓存写入失败: error_type={type(exc).__name__}"
                        )
                if delivery_status is DeliveryStatus.UNKNOWN:
                    logger.warning(f"{LOG_PREFIX} 签到卡送达未知，保留背景与缓存: delivery_status=unknown retry=no")
                    if submission_cancelled is not None:
                        raise submission_cancelled
                    return
                try:
                    stage = "usage_record"
                    await self._record_checkin_background(event, background)
                except asyncio.CancelledError:
                    logger.warning(
                        f"{LOG_PREFIX} 签到背景使用记录被取消，保留已发送图片占用"
                    )
                    raise
                except Exception as exc:
                    logger.warning(
                        f"{LOG_PREFIX} 签到背景使用记录失败，保留已发送图片占用: "
                        f"error_type={type(exc).__name__}"
                    )
                logger.info(
                    f"{LOG_PREFIX} 签到卡发送调用已结束: tier={actual_tier} "
                    f"cache_hit={'yes' if cache_hit else 'no'} "
                    f"delivery_status={delivery_status.value} "
                    f"background_mode={getattr(background, 'mode', 'none')}"
                )
                return
        except Exception as e:
            card_path = None
            logger.warning(
                f"{LOG_PREFIX} 签到卡回退纯文字: stage={stage} "
                f"preferred_tier={preferred_tier} "
                f"error_type={type(e).__name__}"
            )
        finally:
            try:
                if claim_held and self._checkin_background_claims_enabled():
                    can_release_claim = True
                    if background_persisted:
                        try:
                            await self.checkin_store.update_record_background(
                                user_id=user_id,
                                date_key=record.date_key,
                                mode="fallback",
                                source="fallback",
                                illust_id="",
                                title="",
                                author="",
                                quality="",
                            )
                            logger.debug(
                                f"{LOG_PREFIX} 未发送签到背景持久化已撤销"
                            )
                        except asyncio.CancelledError:
                            logger.warning(
                                f"{LOG_PREFIX} 未发送签到背景撤销被取消，"
                                f"保留去重占用"
                            )
                            raise
                        except Exception as exc:
                            can_release_claim = False
                            logger.warning(
                                f"{LOG_PREFIX} 未发送签到背景撤销失败，"
                                f"保留去重占用: "
                                f"error_type={type(exc).__name__}"
                            )
                    if can_release_claim:
                        if cached_path is not None:
                            try:
                                Path(cached_path).unlink(missing_ok=True)
                            except OSError as exc:
                                logger.warning(
                                    f"{LOG_PREFIX} 未发送签到卡缓存清理失败: "
                                    f"error_type={type(exc).__name__}"
                                )
                        await self._release_checkin_background_claim(
                            event, background
                        )
                        claim_held = False
            finally:
                if (
                    background
                    and background.image_path
                    and background.mode == "pixiv_daily"
                ):
                    cleanup(background.image_path)
                    if not Path(background.image_path).exists():
                        logger.debug(
                            f"{LOG_PREFIX} 签到背景临时文件清理完成: "
                            f"mode=pixiv_daily"
                        )
        yield event.plain_result(self._format_checkin_plain_text(result))

    async def _grant_checkin_omnidraw_quota(
        self, event: AstrMessageEvent, user_id: str
    ) -> str:
        """首签成功后顺便给万象画卷发当日生图额度（替代被 stop_event 屏蔽的对方 /签到）。

        失败时静默返回空串，不阻断签到主流程。
        """
        bridge = getattr(self, "_omnidraw_bridge", None)
        if bridge is None:
            return ""
        try:
            status = bridge.snapshot(user_id, str(event.get_group_id() or ""))
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 签到联动额度预检失败: "
                f"user_id={user_id} error_type={type(exc).__name__}"
            )
            return ""
        if not status.available or not status.checkin_enabled:
            return ""
        display_name = str(event.get_sender_name() or "")
        granted = await bridge.grant_daily_checkin_bonus(
            user_id, display_name=display_name
        )
        if not granted.granted:
            return ""
        return f"\n{granted.message}（当日有效，剩余 {granted.remaining} 张）"

    async def _prepare_checkin_record_content(
        self,
        event: AstrMessageEvent,
        record: CheckinRecord,
        *,
        allow_ai: bool,
    ) -> CheckinRecord:
        if record.greeting:
            return record
        content, _title = await self._compose_checkin_content(
            event,
            record,
            self._checkin_profile_from_record(record),
            mutate_features=True,
        )
        local_record = await self.checkin_store.update_record_content(
            user_id=record.user_id,
            date_key=record.date_key,
            event_key=content.event_key,
            event_label=content.event_label,
            greeting=content.greeting,
            greeting_source="local",
            secondary_note=content.secondary_note,
            template_version=record.template_version or "default:1",
        )
        if not allow_ai:
            return local_record
        greeting, source, greeting_attribution = await self._generate_checkin_greeting(
            event, content
        )
        if source not in ("ai", "hitokoto"):
            return local_record
        try:
            return await self.checkin_store.update_record_content(
                user_id=record.user_id,
                date_key=record.date_key,
                event_key=content.event_key,
                event_label=content.event_label,
                greeting=greeting,
                greeting_source=source,
                greeting_attribution=greeting_attribution,
                secondary_note=content.secondary_note,
                template_version=record.template_version or "default:1",
            )
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 远程签到问候升级失败，继续使用本地问候: "
                f"source={source} stage=persist_upgrade "
                f"error_type={type(exc).__name__}"
            )
            return local_record

    async def _compose_checkin_content(
        self,
        event: AstrMessageEvent,
        record: CheckinRecord,
        profile: CheckinProfile,
        *,
        mutate_features: bool,
    ) -> tuple[CheckinContent, str]:
        store = self.checkin_store
        preference = None
        unlocked_ids: tuple[str, ...] = ()
        if (
            store is not None
            and mutate_features
            and all(
                hasattr(store, name)
                for name in ("get_user_preference", "unlock_achievements")
            )
        ):
            preference = await self._ensure_checkin_birthday(event, record.user_id)
            unlocked_ids = await store.unlock_achievements(profile)
            preference = await store.get_user_preference(record.user_id)
        elif store is not None and hasattr(store, "find_user_preference"):
            preference = await store.find_user_preference(record.user_id)

        title = (
            str(ACHIEVEMENTS[preference.selected_title_id]["title"])
            if preference is not None and preference.selected_title_id in ACHIEVEMENTS
            else ""
        )
        birthday_label = ""
        if (
            preference is not None
            and preference.birthday_label
            and birthday_matches(
                record.date_key, preference.birthday_month, preference.birthday_day
            )
        ):
            birthday_label = "生日"

        global_events = (
            await store.events_for_date(record.date_key)
            if store is not None and hasattr(store, "events_for_date")
            else ()
        )
        content = resolve_checkin_content(
            record,
            profile,
            birthday_label=birthday_label,
            custom_event_label=global_events[0].name if global_events else "",
            online_holiday=(
                calendar.lookup(record.date_key)
                if (calendar := getattr(self, "holiday_calendar", None)) is not None
                else None
            ),
            secondary_event_labels=tuple(item.name for item in global_events[1:]),
            current_title=title,
            unlocked_achievements=tuple(
                str(ACHIEVEMENTS[item]["title"]) for item in unlocked_ids
            ),
        )
        return content, title

    async def _generate_checkin_greeting(
        self, event: AstrMessageEvent, content: CheckinContent
    ) -> tuple[str, str, str]:
        greeting_mode = self._checkin_greeting_mode()
        if greeting_mode == "local":
            return content.greeting, "local", ""
        if greeting_mode == "hitokoto":
            return await self.checkin_greeting.generate_hitokoto(
                content.context,
                timeout=self._cfg_float("checkin_hitokoto_timeout", 5.0, 1.0, 15.0),
                categories=self._cfg_get("checkin_hitokoto_categories", ["全部"]),
            )
        greeting, source = await self.checkin_greeting.generate(
            event,
            content.context,
            enabled=True,
            provider_id=self._cfg_str("checkin_ai_greeting_provider_id", ""),
            prompt=self._cfg_str(
                "checkin_ai_greeting_prompt", DEFAULT_CHECKIN_GREETING_PROMPT
            ),
            timeout=self._cfg_float("checkin_ai_greeting_timeout", 8.0, 1.0, 30.0),
        )
        return greeting, source, ""

    async def _refresh_checkin_hitokoto(
        self, event: AstrMessageEvent, record: CheckinRecord
    ) -> CheckinRecord:
        """Refresh only the Hitokoto greeting, keeping the current greeting on failure."""
        if self._checkin_greeting_mode() != "hitokoto":
            return record
        store = getattr(self, "checkin_store", None)
        refresh = getattr(store, "refresh_record_greeting", None)
        if not callable(refresh):
            return record
        try:
            content, _title = await self._compose_checkin_content(
                event,
                record,
                self._checkin_profile_from_record(record),
                mutate_features=False,
            )
            (
                greeting,
                source,
                attribution,
            ) = await self.checkin_greeting.generate_hitokoto(
                content.context,
                timeout=self._cfg_float("checkin_hitokoto_timeout", 5.0, 1.0, 15.0),
                categories=self._cfg_get("checkin_hitokoto_categories", ["全部"]),
            )
            if source != "hitokoto" or not greeting:
                return record
            return await refresh(
                user_id=record.user_id,
                date_key=record.date_key,
                greeting=greeting,
                greeting_source="hitokoto",
                greeting_attribution=attribution,
            )
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 刷新签到一言失败，保留原问候: "
                f"error_type={type(exc).__name__}"
            )
            return record

    def _checkin_greeting_mode(self) -> str:
        mode = self._cfg_str("checkin_greeting_mode", "hitokoto").lower()
        return mode if mode in ("local", "hitokoto", "ai") else "hitokoto"

    async def _ensure_checkin_birthday(self, event: AstrMessageEvent, user_id: str):
        preference = await self.checkin_store.get_user_preference(user_id)
        if preference.birthday_source == "manual" or preference.qq_birthday_checked:
            return preference
        lookup = await self._fetch_qq_birthday(event, user_id)
        if lookup.definitive:
            await self.checkin_store.mark_qq_birthday_checked(user_id)
        if lookup.value is not None:
            return await self.checkin_store.set_qq_birthday_if_not_manual(
                user_id=user_id, month=lookup.value[0], day=lookup.value[1]
            )
        return await self.checkin_store.get_user_preference(user_id)

    async def _get_checkin_user_title(self, user_id: str) -> str:
        store = self.checkin_store
        if store is None or not hasattr(store, "get_user_preference"):
            return ""
        preference = await store.get_user_preference(user_id)
        if preference.selected_title_id not in ACHIEVEMENTS:
            return ""
        return str(ACHIEVEMENTS[preference.selected_title_id]["title"])

    @staticmethod
    async def _fetch_qq_birthday(
        event: AstrMessageEvent, user_id: str
    ) -> QQBirthdayLookup:
        try:
            if event.get_platform_name() != "aiocqhttp":
                logger.info(
                    f"{LOG_PREFIX} QQ 生日读取跳过: platform={event.get_platform_name()}"
                )
                return QQBirthdayLookup(None, False)
            bot = getattr(event, "bot", None)
            if bot is None or not hasattr(bot, "call_action"):
                logger.warning(
                    f"{LOG_PREFIX} QQ 生日未读取: reason=call_action_unavailable"
                )
                return QQBirthdayLookup(None, False)
            payload = await asyncio.wait_for(
                bot.call_action(
                    action="get_stranger_info",
                    user_id=int(user_id),
                    no_cache=True,
                ),
                timeout=3.0,
            )
            parsed = parse_qq_birthday(payload)
            if parsed is None:
                logger.info(f"{LOG_PREFIX} QQ 生日未读取: reason=birthday_not_available")
            else:
                logger.info(f"{LOG_PREFIX} QQ 生日读取成功")
            return QQBirthdayLookup(parsed, True)
        except asyncio.TimeoutError:
            logger.debug(f"{LOG_PREFIX} QQ 生日读取跳过: reason=timeout")
            return QQBirthdayLookup(None, False)
        except (TypeError, ValueError) as exc:
            logger.warning(
                f"{LOG_PREFIX} QQ 生日资料解析失败: "
                f"error_type={type(exc).__name__}"
            )
            return QQBirthdayLookup(None, False)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} QQ 生日读取异常: "
                f"error_type={type(exc).__name__}"
            )
            return QQBirthdayLookup(None, False)

    @staticmethod
    def _checkin_profile_from_record(record: CheckinRecord) -> CheckinProfile:
        return CheckinProfile(
            user_id=record.user_id,
            coins=record.total_coins_after,
            affection=record.total_affection_after,
            total_days=record.total_days_after,
            streak_days=record.streak_days_after,
            last_checkin_date=record.date_key,
            boost_start_date="",
            boost_until_date="",
            repeat_penalty_date="",
            repeat_penalty_total=0.0,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )

    @staticmethod
    def _checkin_background_from_record(record: CheckinRecord) -> CardBackground:
        return CardBackground(
            mode=record.background_mode or "fallback",
            source=record.background_source,
            illust_id=record.background_illust_id,
            title=record.background_title,
            author=record.background_author,
            quality=str(getattr(record, "background_quality", "") or ""),
        )

    def _checkin_card_cache_key(
        self,
        event: AstrMessageEvent,
        *,
        profile: CheckinProfile,
        record: CheckinRecord,
        background: CardBackground | None,
        bot_name: str,
        user_title: str = "",
        render_tier: str | None = None,
        policy=None,
        safety_identity=None,
    ) -> str:
        background = background or self._checkin_background_from_record(record)
        identity_background = CardBackground(
            mode=background.mode,
            source=background.source,
            illust_id=background.illust_id,
            title=background.title,
            author=background.author,
            quality=background.quality,
        )
        avatar_url = (
            self._checkin_avatar_url(event)
            if self._cfg_bool("checkin_avatar_enabled", True)
            else ""
        )
        view_model = build_checkin_card_data(
            profile=profile,
            record=record,
            bot_name=bot_name,
            avatar_url=avatar_url,
            background=identity_background,
            user_title=user_title,
            background_refresh_cost=self._cfg_int(
                "checkin_background_refresh_cost", 100, 0, 300
            ),
        )
        view_model["background_mode"] = identity_background.mode
        view_model["background_source"] = identity_background.source
        render_spec = get_checkin_render_tier(
            render_tier or self._record_checkin_render_tier(record)
        )
        view_model["render_tier"] = render_spec.name
        view_model["resource_version"] = 3
        view_model["render_quality"] = CHECKIN_JPEG_QUALITY
        view_model["background_quality"] = str(
            background.quality or render_spec.background_quality
        )
        if safety_identity is not None:
            view_model["content_safety_policy"] = safety_identity
        elif policy is not None:
            view_model["content_safety_policy"] = policy.cache_identity()
        return self.checkin_cache.cache_key(
            date_key=record.date_key,
            user_id=record.user_id,
            template_version=get_checkin_theme(record.theme_id).template_version,
            view_model=view_model,
        )

    async def _persist_checkin_render_tier(
        self,
        record: CheckinRecord,
        render_tier: str,
    ) -> CheckinRecord:
        normalized = get_checkin_render_tier(render_tier).name
        if self._record_checkin_render_tier(record) == normalized:
            return record
        updater = getattr(self.checkin_store, "update_record_render_tier", None)
        if callable(updater):
            return await updater(
                user_id=record.user_id,
                date_key=record.date_key,
                render_tier=normalized,
            )
        return replace(record, render_tier=normalized)
