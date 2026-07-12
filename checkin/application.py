from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent

from .models import ACHIEVEMENTS, CheckinProfile, CheckinRecord
from .birthday import birthday_matches, parse_qq_birthday
from .card import CardBackground, build_checkin_card_data
from .content import resolve_checkin_content
from .greeting import DEFAULT_CHECKIN_GREETING_PROMPT
try:
    from ..pixiv.downloader import cleanup
except ImportError:  # Direct imports used by the test suite.
    from pixiv.downloader import cleanup


LOG_PREFIX = "[GetPx]"
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0


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

        if not _flow_locked:
            locks = getattr(self, "_checkin_flow_locks", None)
            if locks is None:
                locks = {}
                self._checkin_flow_locks = locks
            lock_key = user_id
            lock = locks.setdefault(lock_key, asyncio.Lock())
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
            result = await self.checkin_store.checkin(
                user_id=user_id,
                username=username,
                bot_name=bot_name,
            )
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 签到写入失败: {e}")
            yield event.plain_result(f"签到失败: {e}")
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
            logger.warning(f"{LOG_PREFIX} 签到内容持久化失败，回退纯文字: {e}")
            yield event.plain_result(self._format_checkin_plain_text(result))
            return

        cache = getattr(self, "checkin_cache", None)
        if cache is None:
            yield event.plain_result(self._format_checkin_plain_text(result))
            return

        background: CardBackground | None = None
        card_path: Path | None = None
        claim_held = False
        profile_snapshot = self._checkin_profile_from_record(record)
        user_title = await self._get_checkin_user_title(record.user_id)
        try:
            if result.duplicate:
                background = self._checkin_background_from_record(record)
            else:
                background = await self._prepare_checkin_background(event, record)
                claim_held = bool(
                    background is not None
                    and background.mode == "pixiv_daily"
                    and background.illust_id
                )

            cache_key = self._checkin_card_cache_key(
                event,
                profile=profile_snapshot,
                record=record,
                background=background,
                bot_name=bot_name,
                user_title=user_title,
            )
            cached_path = cache.get(record.date_key, cache_key)
            if cached_path is None and result.duplicate:
                background = await self._restore_checkin_background(event, record)

            renderer_source_path = ""

            async def render_card() -> str:
                nonlocal renderer_source_path
                renderer_source_path = await self._render_checkin_card(
                    event,
                    profile=profile_snapshot,
                    record=record,
                    background=background,
                    bot_name=bot_name,
                    user_title=user_title,
                )
                return renderer_source_path

            if cached_path is None:
                try:
                    cached_path = await cache.store(
                        record.date_key,
                        cache_key,
                        render_card,
                    )
                finally:
                    cleanup(renderer_source_path)

            if not result.duplicate and background is not None:
                await self.checkin_store.update_record_background(
                    user_id=user_id,
                    date_key=record.date_key,
                    mode=background.mode,
                    source=background.source,
                    illust_id=background.illust_id,
                    title=background.title,
                    author=background.author,
                )
            card_path = cached_path
            if card_path:
                content = [Image.fromFileSystem(str(card_path))]
                if background and background.pixiv_caption:
                    content.append(Plain(background.pixiv_caption))
                await event.send(event.chain_result(content))
                await self._record_checkin_background(event, background)
                claim_held = False
                return
        except Exception as e:
            card_path = None
            logger.warning(f"{LOG_PREFIX} 签到卡片渲染失败，回退纯文字: {e}")
        finally:
            try:
                if claim_held:
                    await self._release_checkin_background_claim(event, background)
                    claim_held = False
            finally:
                if (
                    background
                    and background.image_path
                    and background.mode == "pixiv_daily"
                ):
                    cleanup(background.image_path)
        yield event.plain_result(self._format_checkin_plain_text(result))



    async def _prepare_checkin_record_content(
        self,
        event: AstrMessageEvent,
        record: CheckinRecord,
        *,
        allow_ai: bool,
    ) -> CheckinRecord:
        if record.greeting:
            return record

        feature_store_available = all(
            hasattr(self.checkin_store, name)
            for name in ("get_user_preference", "unlock_achievements", "events_for_date")
        )
        preference = None
        unlocked_ids: tuple[str, ...] = ()
        title = ""
        if feature_store_available:
            preference = await self._ensure_checkin_birthday(event, record.user_id)
            unlocked_ids = await self.checkin_store.unlock_achievements(
                self._checkin_profile_from_record(record)
            )
            preference = await self.checkin_store.get_user_preference(record.user_id)
            title = (
                str(ACHIEVEMENTS[preference.selected_title_id]["title"])
                if preference.selected_title_id in ACHIEVEMENTS
                else ""
            )
        birthday_label = ""
        if preference is not None and preference.birthday_label and birthday_matches(
            record.date_key, preference.birthday_month, preference.birthday_day
        ):
            birthday_label = "生日"
        global_events = (
            await self.checkin_store.events_for_date(record.date_key)
            if feature_store_available else ()
        )
        custom_event_label = global_events[0].name if global_events else ""
        extra_global_labels = tuple(item.name for item in global_events[1:])

        content = resolve_checkin_content(
            record,
            self._checkin_profile_from_record(record),
            birthday_label=birthday_label,
            custom_event_label=custom_event_label,
            online_holiday=(
                calendar.lookup(record.date_key)
                if (calendar := getattr(self, "holiday_calendar", None)) is not None
                else None
            ),
            secondary_event_labels=extra_global_labels,
            current_title=title,
            unlocked_achievements=tuple(
                str(ACHIEVEMENTS[item]["title"]) for item in unlocked_ids
            ),
        )
        record = await self.checkin_store.update_record_content(
            user_id=record.user_id,
            date_key=record.date_key,
            event_key=content.event_key,
            event_label=content.event_label,
            greeting=content.greeting,
            greeting_source="local",
            secondary_note=content.secondary_note,
            template_version=record.template_version or "v2",
        )
        if not allow_ai:
            return record

        greeting_mode = self._checkin_greeting_mode()
        if greeting_mode == "local":
            return record
        greeting_attribution = ""
        if greeting_mode == "hitokoto":
            greeting, source, greeting_attribution = (
                await self.checkin_greeting.generate_hitokoto(
                    content.context,
                    timeout=self._cfg_float(
                        "checkin_hitokoto_timeout",
                        5.0,
                        1.0,
                        15.0,
                    ),
                )
            )
        else:
            greeting, source = await self.checkin_greeting.generate(
                event,
                content.context,
                enabled=True,
                provider_id=self._cfg_str("checkin_ai_greeting_provider_id", ""),
                prompt=self._cfg_str(
                    "checkin_ai_greeting_prompt",
                    DEFAULT_CHECKIN_GREETING_PROMPT,
                ),
                timeout=self._cfg_float(
                    "checkin_ai_greeting_timeout",
                    8.0,
                    1.0,
                    30.0,
                ),
            )
        if source not in ("ai", "hitokoto"):
            return record
        return await self.checkin_store.update_record_content(
            user_id=record.user_id,
            date_key=record.date_key,
            event_key=content.event_key,
            event_label=content.event_label,
            greeting=greeting,
            greeting_source=source,
            greeting_attribution=greeting_attribution,
            secondary_note=content.secondary_note,
            template_version=record.template_version or "v2",
        )


    def _checkin_greeting_mode(self) -> str:
        mode = self._cfg_str("checkin_greeting_mode", "auto").lower()
        if mode not in ("auto", "local", "ai", "hitokoto"):
            mode = "auto"
        if mode == "auto":
            return (
                "ai"
                if self._cfg_bool("checkin_ai_greeting_enabled", False)
                else "local"
            )
        return mode



    async def _ensure_checkin_birthday(self, event: AstrMessageEvent, user_id: str):
        preference = await self.checkin_store.get_user_preference(user_id)
        if preference.birthday_source == "manual" or preference.qq_birthday_checked:
            return preference
        parsed = await self._fetch_qq_birthday(event, user_id)
        await self.checkin_store.mark_qq_birthday_checked(user_id)
        if parsed is not None:
            return await self.checkin_store.set_qq_birthday_if_not_manual(
                user_id=user_id, month=parsed[0], day=parsed[1]
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
    ) -> tuple[int, int] | None:
        try:
            if event.get_platform_name() != "aiocqhttp":
                logger.info(
                    f"{LOG_PREFIX} QQ 生日读取跳过: platform={event.get_platform_name()}"
                )
                return None
            bot = getattr(event, "bot", None)
            if bot is None or not hasattr(bot, "call_action"):
                logger.warning(f"{LOG_PREFIX} QQ 生日读取失败: 当前事件不支持 call_action")
                return None
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
                logger.info(f"{LOG_PREFIX} QQ 生日未读取: 用户未公开生日")
            else:
                logger.info(f"{LOG_PREFIX} QQ 生日读取成功: user_id={user_id}")
            return parsed
        except asyncio.TimeoutError:
            logger.warning(f"{LOG_PREFIX} QQ 生日读取超时: user_id={user_id}")
            return None
        except (TypeError, ValueError) as exc:
            logger.warning(f"{LOG_PREFIX} QQ 生日资料解析失败: {exc}")
            return None
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} QQ 生日读取异常: {exc}", exc_info=True)
            return None



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
    ) -> str:
        background = background or self._checkin_background_from_record(record)
        identity_background = CardBackground(
            mode=background.mode,
            source=background.source,
            illust_id=background.illust_id,
            title=background.title,
            author=background.author,
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
        )
        view_model["background_mode"] = identity_background.mode
        view_model["background_source"] = identity_background.source
        view_model["render_quality"] = self._cfg_int(
            "checkin_card_quality", 95, 60, 100
        )
        return self.checkin_cache.cache_key(
            date_key=record.date_key,
            user_id=record.user_id,
            template_version=record.template_version or "v2",
            view_model=view_model,
        )
