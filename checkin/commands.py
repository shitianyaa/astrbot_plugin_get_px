from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import re
import time

from astrbot.api.all import File, Image, Plain, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.star.star_tools import StarTools

from .models import (
    ACHIEVEMENTS,
    BOOST_MULTIPLIER,
    STREAK_AFFECTION_BONUS,
    STREAK_AFFECTION_BONUS_MAX,
    STREAK_COIN_BONUS,
    STREAK_COIN_BONUS_MAX,
    STREAK_STEP_DAYS,
    CheckinProfile,
    CheckinRecord,
    CheckinResult,
)
from .rules import (
    affection_level,
    boost_status_text,
    daily_base_reward,
    daily_note,
    is_boost_active,
    parse_date,
)
from .store import CheckinStore
from .birthday import parse_month_day
from .card import CardBackground
from .snapshot import dump_checkin_snapshot_json
from .themes import get_checkin_theme

try:
    from ..pixiv.downloader import cleanup
except ImportError:  # Direct imports used by the test suite.
    from pixiv.downloader import cleanup


LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
MAX_CHECKIN_BACKUP_BYTES = 5 * 1024 * 1024
MAX_CHECKIN_BACKUP_FILES = 50


class CheckinCommandMixin:
    """Implement user-facing check-in commands without AstrBot decorators."""

    def _plugin_data_dir(self) -> Path:
        data_dir = self.data_dir
        if data_dir is None:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
            self.data_dir = data_dir
        return Path(data_dir)

    def _checkin_backup_dir(self) -> Path:
        backup_dir = self._plugin_data_dir() / "checkin_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        return backup_dir

    async def _write_checkin_snapshot_backup(self, *, prefix: str) -> Path:
        if self.checkin_store is None:
            raise RuntimeError("签到数据尚未初始化")
        snapshot = await self.checkin_store.export_snapshot()
        return self._write_checkin_snapshot_file(snapshot, prefix=prefix)

    def _write_checkin_snapshot_file(
        self, snapshot: dict[str, object], *, prefix: str
    ) -> Path:
        backup_dir = self._checkin_backup_dir()
        payload = dump_checkin_snapshot_json(snapshot)
        for _ in range(100):
            file_path = backup_dir / f"{prefix}-{time.time_ns()}.json"
            try:
                with file_path.open("x", encoding="utf-8") as handle:
                    handle.write(payload)
                self._prune_checkin_backups(keep=file_path)
                return file_path
            except FileExistsError:
                continue
        raise RuntimeError("无法创建唯一的签到备份文件")

    def _prune_checkin_backups(self, *, keep: Path | None = None) -> None:
        backup_dir = self._checkin_backup_dir()
        try:
            candidates = sorted(
                (
                    item
                    for item in backup_dir.glob("*.json")
                    if item.is_file() and not item.name.startswith(".upload-")
                ),
                key=lambda item: (item.stat().st_mtime_ns, item.name),
                reverse=True,
            )
        except OSError as exc:
            logger.warning(
                f"{LOG_PREFIX} 签到备份扫描失败: error_type={type(exc).__name__}"
            )
            return
        retained = set(candidates[:MAX_CHECKIN_BACKUP_FILES])
        if keep is not None:
            retained.add(keep)
        removed = 0
        for item in candidates:
            if item in retained:
                continue
            try:
                item.unlink(missing_ok=True)
                removed += 1
            except OSError as exc:
                logger.warning(
                    f"{LOG_PREFIX} 签到旧备份清理失败: "
                    f"file={item.name} error_type={type(exc).__name__}"
                )
        if removed:
            logger.info(
                f"{LOG_PREFIX} 签到旧备份已清理: removed_count={removed}"
            )

    async def _read_uploaded_file_bytes(self, upload) -> bytes:
        filename = str(getattr(upload, "filename", "") or "").strip() or "upload.json"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)
        temp_path = self._checkin_backup_dir() / f".upload-{time.time_ns()}-{safe_name}"
        content_length = getattr(upload, "content_length", None)
        if (
            isinstance(content_length, int)
            and content_length > MAX_CHECKIN_BACKUP_BYTES
        ):
            raise ValueError("签到备份文件不能超过 5 MiB")
        try:
            total = 0
            with temp_path.open("xb") as handle:
                while chunk := upload.stream.read(64 * 1024):
                    total += len(chunk)
                    if total > MAX_CHECKIN_BACKUP_BYTES:
                        raise ValueError("签到备份文件不能超过 5 MiB")
                    handle.write(chunk)
            return temp_path.read_bytes()
        finally:
            cleanup(str(temp_path))

    async def _handle_checkin_preview(self, event: AstrMessageEvent):
        if self.checkin_store is None:
            yield event.plain_result("签到数据尚未初始化，请稍后再试")
            return
        user_id = str(event.get_sender_id() or "debug")
        username = self._event_username(event, user_id)
        bot_name = self._cfg_str("checkin_bot_name", "neko") or "neko"
        date_key = CheckinStore.today_key()
        now = CheckinStore.now_iso()
        profile = await self.checkin_store.find_profile(user_id)
        if profile is None:
            profile = CheckinProfile(
                user_id=user_id,
                coins=0,
                affection=0.0,
                total_days=0,
                streak_days=0,
                last_checkin_date="",
                boost_start_date="",
                boost_until_date="",
                repeat_penalty_date="",
                repeat_penalty_total=0.0,
                created_at=now,
                updated_at=now,
            )
        existing_record = await self.checkin_store.get_today_record(user_id)
        record = (
            replace(
                existing_record,
                username=username,
                bot_name=bot_name,
                background_mode="",
                background_source="",
                background_illust_id="",
                background_title="",
                background_author="",
                event_key="",
                event_label="",
                greeting="",
                greeting_source="local",
                greeting_attribution="",
                secondary_note="",
            )
            if existing_record is not None
            else self._build_checkin_preview_record(
                profile=profile,
                date_key=date_key,
                now=now,
                username=username,
                bot_name=bot_name,
            )
        )
        content, user_title = await self._compose_checkin_content(
            event,
            record,
            self._checkin_profile_from_record(record),
            mutate_features=False,
        )
        try:
            greeting, source, attribution = await self._generate_checkin_greeting(
                event, content
            )
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 签到预览问候生成失败，使用本地问候: "
                f"preview=true error_type={type(exc).__name__}"
            )
            greeting, source, attribution = content.greeting, "local", ""
        logger.debug(
            f"{LOG_PREFIX} 签到预览问候完成: preview=true result={source}"
        )
        record = replace(
            record,
            event_key=content.event_key,
            event_label=content.event_label,
            greeting=greeting,
            greeting_source=source,
            greeting_attribution=attribution,
            secondary_note=content.secondary_note,
        )
        preview_profile = self._checkin_profile_from_record(record)
        result = CheckinResult(
            profile=preview_profile,
            record=record,
            duplicate=existing_record is not None,
        )

        background: CardBackground | None = None
        card_path = ""
        preview_tier = self._configured_checkin_render_tier()
        try:
            background = await self._prepare_checkin_background(
                event,
                record,
                claim_usage=False,
                refresh_preview=True,
                render_tier=preview_tier,
            )
            rendered_path, _actual_tier = await self._render_checkin_card_with_fallback(
                event,
                profile=preview_profile,
                record=record,
                background=background,
                bot_name=bot_name,
                user_title=user_title,
                preferred_tier=preview_tier,
            )
            card_path = str(rendered_path)
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX} 签到测试卡片渲染失败，回退纯文字: "
                f"preview=true error_type={type(e).__name__}"
            )

        if card_path:
            try:
                content = [
                    Plain("签到测试预览（仅管理员，不写入签到数据）"),
                    Image.fromFileSystem(card_path),
                ]
                if background and background.pixiv_caption:
                    content.append(Plain(background.pixiv_caption))
                await event.send(event.chain_result(content))
                return
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} 签到测试卡片发送失败，回退纯文字: "
                    f"preview=true error_type={type(e).__name__}"
                )
            finally:
                cleanup(card_path)
                if (
                    background
                    and background.image_path
                    and background.mode == "pixiv_daily"
                ):
                    cleanup(background.image_path)

        if background and background.image_path and background.mode == "pixiv_daily":
            cleanup(background.image_path)
        yield event.plain_result(
            "签到测试预览（未写入数据）\n" + self._format_checkin_plain_text(result)
        )

    @staticmethod
    def _build_checkin_preview_record(
        *,
        profile: CheckinProfile,
        date_key: str,
        now: str,
        username: str,
        bot_name: str,
    ) -> CheckinRecord:
        today = date.fromisoformat(date_key)
        previous_date = parse_date(profile.last_checkin_date)
        streak_days = (
            profile.streak_days + 1 if previous_date == today - timedelta(days=1) else 1
        )
        base_coins, base_affection = daily_base_reward(profile.user_id, date_key)
        streak_steps = streak_days // STREAK_STEP_DAYS
        bonus_coins = min(streak_steps * STREAK_COIN_BONUS, STREAK_COIN_BONUS_MAX)
        bonus_affection = min(
            streak_steps * STREAK_AFFECTION_BONUS,
            STREAK_AFFECTION_BONUS_MAX,
        )
        boost_active = is_boost_active(profile, date_key)
        affection_reward = round(base_affection + bonus_affection, 2)
        if boost_active:
            affection_reward = round(affection_reward * BOOST_MULTIPLIER, 2)
        coins_reward = base_coins + bonus_coins
        return CheckinRecord(
            date_key=date_key,
            user_id=profile.user_id,
            username=username,
            bot_name=bot_name,
            base_coins=base_coins,
            bonus_coins=bonus_coins,
            coins_reward=coins_reward,
            base_affection=base_affection,
            bonus_affection=bonus_affection,
            affection_reward=affection_reward,
            boost_active=boost_active,
            boost_multiplier=BOOST_MULTIPLIER if boost_active else 1.0,
            total_coins_after=profile.coins + coins_reward,
            total_affection_after=round(profile.affection + affection_reward, 2),
            total_days_after=profile.total_days + 1,
            streak_days_after=streak_days,
            note=daily_note(profile.user_id, date_key, streak_days),
            background_mode="",
            background_source="",
            background_illust_id="",
            background_title="",
            background_author="",
            created_at=now,
            updated_at=now,
        )

    async def _handle_checkin_export(self, event: AstrMessageEvent):
        if self.checkin_store is None:
            return event.plain_result("签到数据尚未初始化，请稍后再试")
        try:
            export_path = await self._write_checkin_snapshot_backup(
                prefix="checkin-export"
            )
        except Exception as e:
            logger.error(
                f"{LOG_PREFIX} 导出签到备份失败: "
                f"error_type={type(e).__name__}"
            )
            return event.plain_result("导出签到备份失败，请稍后再试")
        try:
            await event.send(
                event.chain_result([File(name=export_path.name, file=str(export_path))])
            )
            return None
        except Exception as e:
            logger.error(
                f"{LOG_PREFIX} 发送签到备份文件失败: "
                f"error_type={type(e).__name__}"
            )
            return event.plain_result("发送签到备份文件失败，请稍后再试")

    async def _handle_checkin_ranking(
        self, event: AstrMessageEvent, mode: str = ""
    ) -> str:
        if self.checkin_store is None:
            return "签到数据尚未初始化，请稍后再试"
        group_id, group_name, _platform = self._event_group_context(event)
        if not group_id:
            return "签到排行只能在群聊中查看"
        aliases = {
            "": "today",
            "今日": "today",
            "今日榜": "today",
            "月榜": "month",
            "月度": "month",
            "连签": "streak",
            "连续": "streak",
            "累计": "total",
            "总榜": "total",
        }
        ranking_type = aliases.get(str(mode or "").strip())
        if ranking_type is None:
            return "请使用：签到排行 今日、月榜、连签或累计"
        result = await self.checkin_store.get_group_ranking(
            group_id=group_id,
            ranking_type=ranking_type,
            limit=10,
        )
        titles = {
            "today": "今日签到",
            "month": "本月签到",
            "streak": "连续签到",
            "total": "累计签到",
        }
        units = {"month": "天", "streak": "天", "total": "天"}
        lines = [f"{group_name} · {titles[ranking_type]}排行"]
        entries = result["entries"]
        if not entries:
            return lines[0] + "\n还没有签到记录"
        for entry in entries:
            if ranking_type == "today":
                value = str(entry["value"])[11:19]
            else:
                value = f"{entry['value']}{units[ranking_type]}"
            lines.append(f"{entry['rank']:>2}. {entry['username']}  {value}")
        sender_id = str(event.get_sender_id() or "")
        own = next(
            (item for item in result["all_entries"] if item["user_id"] == sender_id),
            None,
        )
        if own is None:
            lines.append("\n你暂未进入本榜")
        elif int(own["rank"]) > 10:
            lines.append(f"\n你的名次：第 {own['rank']} 名")
        return "\n".join(lines)

    async def _handle_checkin_status(self, event: AstrMessageEvent):
        if not self._cfg_bool("checkin_enabled", True):
            yield event.plain_result("签到功能已关闭")
            return
        if self.checkin_store is None:
            yield event.plain_result("签到数据尚未初始化，请稍后再试")
            return
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            yield event.plain_result("无法识别用户 ID，暂时不能查看签到状态")
            return
        try:
            profile = await self.checkin_store.get_profile(user_id)
            preference = await self.checkin_store.get_user_preference(user_id)
        except Exception as e:
            logger.warning(
                f"{LOG_PREFIX} 读取签到状态失败: "
                f"error_type={type(e).__name__}"
            )
            yield event.plain_result("读取签到状态失败，请稍后再试")
            return
        level = affection_level(profile.affection)
        today = CheckinStore.today_key()
        signed_today = profile.last_checkin_date == today
        lines = [
            "签到状态",
            f"UID: {profile.user_id}",
            f"今日: {'已签到' if signed_today else '未签到'}",
            f"累计签到: {profile.total_days} 天",
            f"连续签到: {profile.streak_days} 天",
            f"金币: {profile.coins}",
            f"签到主题: {get_checkin_theme(preference.current_theme_id).name}",
            f"好感度: {profile.affection:.2f}（{level['name']}）",
            f"好感度加持: {boost_status_text(profile, today)}",
        ]
        yield event.plain_result("\n".join(lines))

    async def _handle_checkin_birthday(
        self, event: AstrMessageEvent, action: str, value: str
    ) -> str:
        if self.checkin_store is None:
            return "签到数据尚未初始化，请稍后再试"
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            return "无法识别用户 ID"
        action = str(action or "").strip()
        try:
            if action == "设置":
                parsed = parse_month_day(value)
                if parsed is None:
                    return "用法: 签到我的 生日 设置 MM-DD"
                preference = await self.checkin_store.set_birthday(
                    user_id=user_id, month=parsed[0], day=parsed[1], source="manual"
                )
                return f"生日已设置为 {preference.birthday_label}（手动）"
            if action == "清除":
                await self.checkin_store.clear_birthday(user_id)
                return "生日已清除，再次使用“签到我的 生日”会重新读取 QQ 资料"
            if action not in {"", "查看"}:
                return (
                    "用法: 签到我的 生日 [查看]\n"
                    "或: 签到我的 生日 设置 MM-DD\n"
                    "或: 签到我的 生日 清除"
                )
            preference = await self.checkin_store.get_user_preference(user_id)
            if preference.birthday_label:
                source = "手动" if preference.birthday_source == "manual" else "QQ资料"
                return f"当前签到生日: {preference.birthday_label}（{source}）"
            lookup = await self._fetch_qq_birthday(event, user_id)
            if lookup.definitive:
                await self.checkin_store.mark_qq_birthday_checked(user_id)
            if lookup.value is None:
                return (
                    "用户未公开生日"
                    if lookup.definitive
                    else "QQ 生日读取失败，请稍后再试"
                )
            preference = await self.checkin_store.set_qq_birthday_if_not_manual(
                user_id=user_id, month=lookup.value[0], day=lookup.value[1]
            )
            return f"当前签到生日: {preference.birthday_label}（QQ资料）"
        except ValueError as exc:
            return str(exc)

    async def _handle_checkin_achievements(self, event: AstrMessageEvent) -> str:
        if self.checkin_store is None:
            return "签到数据尚未初始化，请稍后再试"
        user_id = str(event.get_sender_id() or "")
        profile = await self.checkin_store.get_profile(user_id)
        newly_unlocked = await self.checkin_store.unlock_achievements(profile)
        unlocked = set(await self.checkin_store.list_achievements(user_id))
        lines = ["签到成就"]
        if newly_unlocked:
            names = "、".join(
                str(ACHIEVEMENTS[item]["title"]) for item in newly_unlocked
            )
            lines.append(f"本次补发: {names}")
        for achievement_id, definition in ACHIEVEMENTS.items():
            value = (
                profile.total_days
                if definition["kind"] == "total"
                else profile.streak_days
            )
            mark = "✓" if achievement_id in unlocked else "·"
            lines.append(
                f"{mark} {definition['title']}（{min(value, definition['threshold'])}/{definition['threshold']}）"
            )
        return "\n".join(lines)

    async def _handle_checkin_titles(self, event: AstrMessageEvent) -> str:
        if self.checkin_store is None:
            return "签到数据尚未初始化，请稍后再试"
        user_id = str(event.get_sender_id() or "")
        profile = await self.checkin_store.get_profile(user_id)
        await self.checkin_store.unlock_achievements(profile)
        preference = await self.checkin_store.get_user_preference(user_id)
        unlocked = await self.checkin_store.list_achievements(user_id)
        lines = ["签到称号"]
        if not unlocked:
            lines.append("尚未解锁称号，完成首次签到即可获得")
        for achievement_id in unlocked:
            title = str(ACHIEVEMENTS[achievement_id]["title"])
            mark = "当前" if achievement_id == preference.selected_title_id else "可用"
            lines.append(f"[{mark}] {title}（{achievement_id}）")
        lines.append("使用“签到我的 称号 佩戴 <称号ID或名称>”切换")
        return "\n".join(lines)

    async def _handle_select_checkin_title(
        self, event: AstrMessageEvent, title: str
    ) -> str:
        if self.checkin_store is None:
            return "签到数据尚未初始化，请稍后再试"
        if not title:
            return "用法: 签到我的 称号 佩戴 <称号ID或名称>"
        try:
            user_id = str(event.get_sender_id() or "")
            profile = await self.checkin_store.get_profile(user_id)
            await self.checkin_store.unlock_achievements(profile)
            title_id = await self.checkin_store.select_title(
                user_id=user_id, title=title
            )
        except ValueError as exc:
            return str(exc)
        return f"已佩戴称号：{ACHIEVEMENTS[title_id]['title']}"

    async def _handle_checkin_event_admin(
        self,
        event: AstrMessageEvent,
        action: str,
        event_type: str,
        date_value: str,
        name: str,
    ) -> str:
        if self.checkin_store is None:
            return "签到数据尚未初始化，请稍后再试"
        if action in {"", "查看", "列表"}:
            events = await self.checkin_store.list_global_events()
            if not events:
                return "当前没有全局签到事件"
            return "\n".join(
                ["全局签到事件"]
                + [
                    f"#{item.event_id} {item.event_type} {item.date_value} {item.name}"
                    for item in events
                ]
            )
        if action == "删除":
            if not event_type.isdigit():
                return "用法: 签到管理 事件 删除 ID"
            deleted = await self.checkin_store.delete_global_event(int(event_type))
            return "事件已删除" if deleted else "未找到该事件"
        if action in {"添加年度", "添加单次"}:
            name = " ".join(part for part in (date_value, name) if part).strip()
            date_value = event_type
            event_type = action.removeprefix("添加")
            action = "添加"
        if action != "添加":
            return "用法: 签到管理 事件 添加 <年度|单次> <日期> <名称>"
        type_map = {"年度": "annual", "单次": "once"}
        if event_type not in type_map or not name:
            return (
                "用法: 签到管理 事件 添加年度 MM-DD 名称\n"
                "或: 签到管理 事件 添加单次 YYYY-MM-DD 名称"
            )
        try:
            item = await self.checkin_store.add_global_event(
                event_type=type_map[event_type],
                date_value=date_value,
                name=name,
                created_by=str(event.get_sender_id() or ""),
            )
        except ValueError as exc:
            return str(exc)
        return f"已添加事件 #{item.event_id}: {item.date_value} {item.name}"

    @staticmethod
    def _format_checkin_plain_text(result) -> str:
        record = result.record
        if record is None:
            return "签到成功"
        level = affection_level(record.total_affection_after)
        heading = (
            f"{record.username} 今日签到记录"
            if result.duplicate
            else f"{record.username} 签到成功"
        )
        return "\n".join(
            [
                heading,
                f"日期: {record.date_key}",
                f"今日奖励: 金币 +{record.coins_reward}，好感度 +{record.affection_reward:.2f}",
                f"累计签到: {record.total_days_after} 天，连续签到: {record.streak_days_after} 天",
                f"金币: {record.total_coins_after}，好感度: {record.total_affection_after:.2f}（{level['name']}）",
                record.greeting or record.note,
            ]
        )
