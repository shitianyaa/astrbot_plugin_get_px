"""AstrBot 插件 — 安全插画发图与签到

通过标签搜索插画并发送图片，支持 Lolicon/Pixiv 图片源优先级与自动回退、内容安全过滤、多页作品、自然语言自动触发和签到。

搜索指令：
    /p [标签] [数量]           搜索并发送图片

自动触发（需在配置中开启）：
    来一份图                   发送 1 张随机图片
    来三张初音ミク图             搜索标签「初音ミク」发送 3 张

签到指令：
    /签到                      每日签到
    /签到帮助                  查看所有签到指令
    /签到状态                  查看金币、好感度和连续签到状态
    /签到成就                  查看已解锁成就和下一目标
    /签到生日                  生日查看/设置/清除
    /签到称号                  称号查看与佩戴
    /签到排行                  查看签到排行
    /签到商店                  访问签到商店
    /签到主题                  签到主题列表、查看、购买与切换
    /签到管理                  签到管理功能（仅管理员）
    刷新背景                   花费金币重新抽取今天的签到背景
    签到日历 [YYYY-MM]         查看个人月度签到日历（默认当月）
"""

# 注意：不要在本模块使用 `from __future__ import annotations`。
# AstrBot 识别 GreedyStr 的规则：
# - 无默认值时：`annotation is GreedyStr`
# - 有默认值时：`default is GreedyStr`（不是看注解）
# 因此这里使用 GreedyStr 类作为默认哨兵，既保留贪婪参数，又支持直接无参调用。
# 字符串化注解会让贪婪参数失效。

import asyncio
from pathlib import Path
import re
import time
import weakref

from astrbot.api import logger
from astrbot.api.all import AstrBotConfig, Image
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_tools import StarTools
from .checkin import CheckinStore, UnversionedCheckinDatabaseError
from .group_safety import GroupSafetyService
from .checkin.application import CheckinApplicationMixin
from .checkin.artwork import CheckinArtworkMixin
from .checkin.cache import CheckinCardCache
from .checkin.commands import CheckinCommandMixin
from .checkin.greeting import CheckinGreetingGenerator
from .checkin.holiday import HolidayCalendar
from .checkin.omnidraw_bridge import OmnidrawBridge
from .checkin.shop import CheckinShopMixin
from .pixiv import DeliveryMixin, FiltersMixin, SearchMixin
from .pixiv.client import PixivClient
from .pixiv.constants import MAX_IMAGE_COUNT
from .pixiv.downloader import ImageDownloader
from .pixiv.index import ImageIndexStore
from .pixiv.lolicon import LoliconClient
from .plugin_api import PluginWebApi

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
PLUGIN_VERSION = "v3.8.7"
WEB_INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试"

AUTO_TRIGGER_PATTERN = r"^/?(来\s*(.*?)(份|个|张|点))(.*?)(福利|色|瑟|涩|塞)?图$"
CHECKIN_REGEX_PATTERN = r"^(?!/)签到$"
CHECKIN_HELP_IMAGE = (
    Path(__file__).resolve().parent / "assets" / "checkin_help_v4.1.png"
)


CHINESE_NUMBER_MAP = {
    "一": "1",
    "二": "2",
    "两": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
    "十": "10",
}

# ──────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────


class GetPxPlugin(
    CheckinApplicationMixin,
    CheckinCommandMixin,
    CheckinShopMixin,
    CheckinArtworkMixin,
    SearchMixin,
    DeliveryMixin,
    FiltersMixin,
    Star,
):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.group_safety_service = GroupSafetyService(config, log_prefix=LOG_PREFIX)
        self.client: PixivClient | None = None
        self.lolicon_client: LoliconClient | None = None
        self.downloader = ImageDownloader(
            self._cfg_str("lolicon_image_proxy_origins", "")
        )
        self._last_request: dict[str, float] = {}
        self.data_dir: Path | None = None
        self.image_index: ImageIndexStore | None = None
        self.plugin_web_api = PluginWebApi(
            self,
            plugin_name=PLUGIN_NAME,
            log_prefix=LOG_PREFIX,
            internal_error_message=WEB_INTERNAL_ERROR_MESSAGE,
        )
        self.checkin_store: CheckinStore | None = None
        self.checkin_cache: CheckinCardCache | None = None
        self.checkin_greeting = CheckinGreetingGenerator(context)
        self.holiday_calendar: HolidayCalendar | None = None
        self._holiday_refresh_task: asyncio.Task | None = None
        self._termination_task: asyncio.Task[None] | None = None
        self._checkin_flow_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
            weakref.WeakValueDictionary()
        )

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────

    async def initialize(self):
        """插件加载时初始化 Pixiv 客户端。"""
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir = Path(data_dir)
        self.downloader.temp_dir = self.data_dir / "temp"
        self._cleanup_retired_transport_config()
        dedupe_days = self._cfg_int("dedupe_days", 1, 0, 7)
        self._init_client()
        # SQLite DDL/迁移是同步操作，放入线程池避免阻塞事件循环
        self.image_index = await asyncio.to_thread(
            ImageIndexStore,
            data_dir,
            retention_days=dedupe_days,
        )
        await self.image_index.cleanup_old_days(trigger="startup")
        checkin_database_existed = (self.data_dir / "checkin.sqlite3").exists()
        try:
            self.checkin_store = await asyncio.to_thread(CheckinStore, data_dir)
        except UnversionedCheckinDatabaseError:
            logger.error(
                f"{LOG_PREFIX} 签到数据库缺少 schema 版本号且已包含数据表，"
                "无法确认其格式。如果是从 v2.8.x 升级，请先使用插件 3.0.0 "
                f"启动一次完成数据迁移，再升级到 {PLUGIN_VERSION}；"
                "否则请检查或移除 checkin.sqlite3 后重启。"
            )
            raise
        database_action = "已加载" if checkin_database_existed else "已创建"
        logger.info(
            f"{LOG_PREFIX} 签到数据库{database_action}: "
            f"version={PLUGIN_VERSION}, path={self.checkin_store._db_path}"
        )
        policy_migration_succeeded = await self.group_safety_service.initialize(
            self.checkin_store
        )
        if policy_migration_succeeded:
            try:
                convergence = await asyncio.to_thread(
                    self.checkin_store.converge_legacy_group_policy_schema
                )
                logger.info(
                    f"{LOG_PREFIX} 会话策略数据库收敛完成: "
                    f"from_version={convergence['from_version']} "
                    f"to_version={convergence['to_version']} "
                    f"backup_path={convergence['backup_path'] or '-'}"
                )
            except Exception as exc:
                logger.error(
                    f"{LOG_PREFIX} 会话策略数据库收敛失败，终止插件加载: "
                    f"error_type={type(exc).__name__}"
                )
                raise
        self.checkin_cache = CheckinCardCache(self.data_dir / "checkin_card_cache")
        self._omnidraw_hint_logged = False
        if self._cfg_bool("checkin_omnidraw_link_enabled", False):
            self._omnidraw_bridge = OmnidrawBridge(self.context)
            self._omnidraw_bridge.log_coexistence_hint()
            self._omnidraw_hint_logged = True
        await asyncio.to_thread(self.checkin_cache.cleanup_expired, force=True)
        self.holiday_calendar = HolidayCalendar(
            self.data_dir,
            plugin_version=PLUGIN_VERSION,
        )
        await self.downloader.resume_cleanup()
        self._holiday_refresh_task = asyncio.create_task(
            self._refresh_holiday_calendar()
        )
        self.plugin_web_api.register()
        logger.info(f"{LOG_PREFIX} 插件已加载: version={PLUGIN_VERSION}")

    async def _refresh_holiday_calendar(self) -> None:
        if self.holiday_calendar is None:
            return
        try:
            updated = await self.holiday_calendar.refresh_if_due()
            if updated:
                logger.info(f"{LOG_PREFIX} 节假日数据已更新")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 节假日数据更新失败，继续使用本地规则: "
                f"error_type={type(exc).__name__}"
            )

    def _init_client(self):
        """初始化固定地址的 Lolicon 和可选的 Pixiv 客户端。"""
        if getattr(self, "lolicon_client", None) is None:
            self.lolicon_client = LoliconClient(
                exclude_ai=self._cfg_bool("lolicon_exclude_ai", True),
                request_timeout=self._cfg_float(
                    "request_timeout", 30.0, 5.0, 120.0
                ),
            )
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.info(f"{LOG_PREFIX} 图片源初始化完成: available_sources=lolicon reason=no_pixiv_token")
            return

        if getattr(self, "client", None) is None:
            self.client = PixivClient(
                refresh_token=token,
                request_timeout=self._cfg_float("request_timeout", 30.0, 5.0, 120.0),
            )
        logger.info(f"{LOG_PREFIX} 图片源初始化完成: available_sources=lolicon,pixiv")

    def _web_api(self) -> PluginWebApi:
        service = getattr(self, "plugin_web_api", None)
        if service is None:
            service = PluginWebApi(
                self,
                plugin_name=PLUGIN_NAME,
                log_prefix=LOG_PREFIX,
                internal_error_message=WEB_INTERNAL_ERROR_MESSAGE,
            )
        return service

    def _web_internal_error(self, action: str, exc: Exception):
        return self._web_api().internal_error(action, exc)

    async def _web_checkin_import(self):
        return await self._web_api().checkin_import()

    @filter.on_plugin_loaded()
    async def on_plugin_loaded(self, metadata) -> None:
        """插件按字母序加载，本插件早于万象画卷初始化；待其加载完成后再补一次共存提示。"""
        if getattr(metadata, "name", "") != "astrbot_plugin_omnidraw":
            return
        bridge = getattr(self, "_omnidraw_bridge", None)
        if bridge is None:
            return
        await asyncio.to_thread(bridge.log_coexistence_hint)

    async def terminate(self):
        """插件卸载/停用时清理资源，并让并发调用等待同一清理任务。"""
        task = self._termination_task
        if task is not None and task.done() and (
            task.cancelled() or task.exception() is not None
        ):
            self._termination_task = None
            task = None
        if task is None:
            task = asyncio.create_task(self._terminate_resources())
            self._termination_task = task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled() and self._termination_task is task:
                self._termination_task = None
            raise
        except Exception:
            if self._termination_task is task:
                self._termination_task = None
            raise

    async def _terminate_resources(self) -> None:
        """执行一次插件资源清理。"""
        plugin_web_api = getattr(self, "plugin_web_api", None)
        if plugin_web_api is not None:
            plugin_web_api.unregister()
        if self._holiday_refresh_task is not None:
            self._holiday_refresh_task.cancel()
            await asyncio.gather(self._holiday_refresh_task, return_exceptions=True)
            self._holiday_refresh_task = None
        if getattr(self, "client", None) is not None:
            try:
                await self.client.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭 Pixiv 客户端失败: "
                    f"error_type={type(exc).__name__}"
                )
            finally:
                self.client = None
        if getattr(self, "lolicon_client", None) is not None:
            try:
                await self.lolicon_client.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭 Lolicon 客户端失败: "
                    f"error_type={type(exc).__name__}"
                )
            finally:
                self.lolicon_client = None
        try:
            await self.downloader.close()
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 关闭图片下载器失败: "
                f"error_type={type(exc).__name__}"
            )
        try:
            await self.checkin_greeting.close()
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 关闭签到问候会话失败: "
                f"error_type={type(exc).__name__}"
            )
        self._last_request.clear()
        locks = getattr(self, "_checkin_flow_locks", None)
        if locks is not None:
            locks.clear()
        if self.image_index is not None:
            try:
                self.image_index.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭图片索引失败: "
                    f"error_type={type(exc).__name__}"
                )
        self.image_index = None
        self.checkin_store = None
        logger.info(f"{LOG_PREFIX} 插件已停止")

    # ──────────────────────────────────────────────────────────────
    # 指令：搜索（主指令）
    # ──────────────────────────────────────────────────────────────

    @filter.command("p")
    async def cmd_p(self, event: AstrMessageEvent, query: GreedyStr = GreedyStr):
        """搜索并发送图片。参数: [标签] [数量]"""
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 图片源暂不可用，请配置 Lolicon API，或填写 pixiv_refresh_token 作为回退"
            )
            return
        event.stop_event()
        # 框架无参时传入空字符串；直接调用时则会保留默认哨兵。
        raw_query = "" if query is GreedyStr else str(query or "")
        tag, count = self._split_tag_and_count(raw_query)
        async for result in self._handle_search(event, tag=tag, count_str=count):
            yield result

    @staticmethod
    def _split_tag_and_count(query: str) -> tuple[str, str]:
        """把 GreedyStr 参数拆成标签与尾部数量；纯数字视为随机发图数量。"""
        tokens = query.split()
        if not tokens:
            return "", ""
        if tokens[-1].isdigit():
            return " ".join(tokens[:-1]), tokens[-1]
        return " ".join(tokens), ""

    # ──────────────────────────────────────────────────────────────
    # 签到指令
    # ──────────────────────────────────────────────────────────────

    @filter.command("签到")
    async def cmd_checkin(self, event: AstrMessageEvent):
        """每日签到。"""
        event.stop_event()
        async for result in self._handle_checkin(event):
            yield result

    @filter.command("签到帮助")
    async def cmd_checkin_help(self, event: AstrMessageEvent):
        """发送签到功能帮助图。"""
        event.stop_event()
        if not CHECKIN_HELP_IMAGE.is_file():
            logger.error(
                f"{LOG_PREFIX} 签到帮助图片不存在: {CHECKIN_HELP_IMAGE}"
            )
            yield event.plain_result("签到帮助图片缺失，请联系管理员重新安装插件")
            return
        yield event.chain_result(
            [Image.fromFileSystem(str(CHECKIN_HELP_IMAGE))]
        )

    @filter.command("签到状态")
    async def cmd_checkin_status(self, event: AstrMessageEvent):
        """查看金币、好感度和连续签到状态。"""
        event.stop_event()
        async for result in self._handle_checkin_status(event):
            yield result

    @filter.command("签到成就")
    async def cmd_checkin_achievements(self, event: AstrMessageEvent):
        """查看已解锁成就和下一目标。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_achievements(event))

    @filter.command_group("签到生日")
    def checkin_birthday(self):
        """生日查看/设置/清除。"""

    @checkin_birthday.command("查看")
    async def cmd_checkin_birthday_view(self, event: AstrMessageEvent):
        """查看生日；未保存时自动读取 QQ 资料。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_birthday(event, "查看", "")
        )

    @checkin_birthday.command("设置")
    async def cmd_checkin_birthday_set(
        self, event: AstrMessageEvent, value: str = ""
    ):
        """手动设置签到生日。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_birthday(event, "设置", value)
        )

    @checkin_birthday.command("清除")
    async def cmd_checkin_birthday_clear(self, event: AstrMessageEvent):
        """清除签到生日。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_birthday(event, "清除", "")
        )

    @filter.command_group("签到称号")
    def checkin_title(self):
        """称号查看与佩戴。"""

    @checkin_title.command("查看")
    async def cmd_checkin_titles(self, event: AstrMessageEvent):
        """查看已解锁的签到称号。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_titles(event))

    @checkin_title.command("佩戴")
    async def cmd_select_checkin_title(self, event: AstrMessageEvent, title: str = ""):
        """佩戴已解锁的签到称号。"""
        event.stop_event()
        yield event.plain_result(await self._handle_select_checkin_title(event, title))

    @filter.command_group("签到排行")
    def checkin_ranking(self):
        """查看当前群的签到排行。"""

    @checkin_ranking.command("今日")
    async def cmd_checkin_ranking_today(self, event: AstrMessageEvent):
        """查看今日签到排行。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_ranking(event, "今日"))

    @checkin_ranking.command("月榜")
    async def cmd_checkin_ranking_month(self, event: AstrMessageEvent):
        """查看本月签到排行。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_ranking(event, "月榜"))

    @checkin_ranking.command("连签")
    async def cmd_checkin_ranking_streak(self, event: AstrMessageEvent):
        """查看连续签到排行。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_ranking(event, "连签"))

    @checkin_ranking.command("累计")
    async def cmd_checkin_ranking_total(self, event: AstrMessageEvent):
        """查看累计签到排行。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_ranking(event, "累计"))

    @filter.regex(CHECKIN_REGEX_PATTERN)
    async def checkin_auto_trigger(self, event: AstrMessageEvent):
        """纯文本触发签到。"""
        if not self._cfg_bool("checkin_enabled", True):
            return
        event.stop_event()
        async for result in self._handle_checkin(event, silent_when_disabled=True):
            yield result

    @filter.command_group("签到商店")
    def checkin_shop(self):
        """签到金币商店。"""

    @checkin_shop.command("查看")
    async def cmd_checkin_shop(self, event: AstrMessageEvent):
        """查看签到商店。"""
        event.stop_event()
        if not self._cfg_bool("checkin_enabled", True):
            yield event.plain_result("签到功能已关闭")
            return
        yield event.plain_result(self._build_checkin_shop())

    @checkin_shop.command("加持")
    async def cmd_buy_checkin_boost(self, event: AstrMessageEvent, days: str = ""):
        """购买好感度双倍加持。"""
        event.stop_event()
        async for result in self._handle_buy_checkin_boost(event, days):
            yield result

    @checkin_shop.command("生图")
    async def cmd_buy_checkin_quota(self, event: AstrMessageEvent, count: str = ""):
        """购买万象画卷生图额度。"""
        event.stop_event()
        async for result in self._handle_buy_checkin_quota(event, count):
            yield result

    @filter.command_group("签到主题")
    def checkin_theme(self):
        """签到主题列表、查看、购买与切换。"""

    @checkin_theme.command("列表")
    async def cmd_checkin_themes(self, event: AstrMessageEvent):
        """查看已购买和可购买的签到主题。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_themes(event))

    @checkin_theme.command("查看")
    async def cmd_preview_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """查看指定签到主题的静态预览图。"""
        event.stop_event()
        yield await self._handle_checkin_theme_preview(event, theme)

    @checkin_theme.command("购买")
    async def cmd_buy_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """购买签到主题，购买成功后自动切换。"""
        event.stop_event()
        yield event.plain_result(await self._handle_buy_checkin_theme(event, theme))

    @checkin_theme.command("切换")
    async def cmd_select_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """切换到默认或已购买的签到主题。"""
        event.stop_event()
        yield event.plain_result(await self._handle_select_checkin_theme(event, theme))

    @filter.command("刷新背景")
    async def cmd_refresh_checkin_background(self, event: AstrMessageEvent):
        """花费金币重新抽取今天的签到背景。"""
        event.stop_event()
        async for result in self._handle_refresh_checkin_background(event):
            yield result

    @filter.command("签到日历")
    async def cmd_checkin_calendar(self, event: AstrMessageEvent, month: str = ""):
        """查看自己的月度签到日历。参数: [YYYY-MM]"""
        event.stop_event()
        async for result in self._handle_checkin_calendar(event, month):
            yield result

    @filter.command_group("签到管理")
    def checkin_admin(self):
        """管理员签到维护功能。"""

    @filter.permission_type(filter.PermissionType.ADMIN)
    @checkin_admin.command("预览")
    async def cmd_checkin_preview(self, event: AstrMessageEvent):
        """用真实用户资料和问候配置预览卡片，不写入签到数据。"""
        event.stop_event()
        async for result in self._handle_checkin_preview(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @checkin_admin.command("导出")
    async def cmd_checkin_export(self, event: AstrMessageEvent):
        """管理员导出签到完整备份。"""
        event.stop_event()
        result = await self._handle_checkin_export(event)
        if result is not None:
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @checkin_admin.command("事件查看")
    async def cmd_checkin_event_list(self, event: AstrMessageEvent):
        """查看全局签到纪念日。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_event_admin(event, "", "", "", "")
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @checkin_admin.command("事件添加")
    async def cmd_checkin_event_add(
        self,
        event: AstrMessageEvent,
        event_type: str = "",
        date_value: str = "",
        name: GreedyStr = GreedyStr,
    ):
        """添加年度或单次全局签到纪念日。"""
        event.stop_event()
        # 框架无参时传入空字符串；直接调用时则会保留默认哨兵。
        raw_name = "" if name is GreedyStr else str(name or "")
        yield event.plain_result(
            await self._handle_checkin_event_admin(
                event, "添加", event_type, date_value, raw_name
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @checkin_admin.command("事件删除")
    async def cmd_checkin_event_delete(
        self, event: AstrMessageEvent, event_id: str = ""
    ):
        """按事件 ID 删除全局签到纪念日。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_event_admin(event, "删除", event_id, "", "")
        )

    @filter.regex(AUTO_TRIGGER_PATTERN)
    async def auto_trigger(self, event: AstrMessageEvent):
        """自然语言自动触发发图。"""
        if not self._cfg_bool("auto_trigger_enabled", False):
            return
        if not self._ensure_client_or_error(event):
            return

        message = event.get_message_str().strip()
        match = re.match(AUTO_TRIGGER_PATTERN, message)
        if not match:
            return

        event.stop_event()

        count_part = match.group(2).strip() if match.group(2) else ""
        tag_part = (match.group(4) or "").strip()

        # 解析数量：中文数字、阿拉伯数字
        count_str = ""
        raw = count_part if count_part else "1"
        if raw.isdigit():
            count_str = raw
        else:
            for cn_digit, arabic in CHINESE_NUMBER_MAP.items():
                if raw == cn_digit:
                    count_str = arabic
                    break
            if not count_str:
                count_str = "1"

        logger.info(
            f"{LOG_PREFIX} 自然语言触发: count={count_str} "
            f"tag_configured={'yes' if tag_part else 'no'}"
        )
        async for result in self._handle_search(
            event, tag=tag_part, count_str=count_str
        ):
            yield result

    # ──────────────────────────────────────────────────────────────
    # 工具方法
    # ──────────────────────────────────────────────────────────────

    def _check_rate_limit(self, user_id: str) -> int:
        """检查用户请求频率，返回需等待秒数（0 表示可立即请求）。"""
        rate_limit = self._cfg_int("rate_limit_seconds", 3, 0, 60)
        if rate_limit <= 0:
            return 0
        now = time.monotonic()
        if len(self._last_request) > 1024:
            cutoff = now - max(float(rate_limit) * 2, 60.0)
            self._last_request = {
                key: timestamp
                for key, timestamp in self._last_request.items()
                if timestamp >= cutoff
            }
        last = self._last_request.get(user_id, 0.0)
        elapsed = now - last
        if elapsed < rate_limit:
            return int(rate_limit - elapsed) + 1
        self._last_request[user_id] = now
        return 0

    def _checkin_flow_lock(self, user_id: str) -> asyncio.Lock:
        locks = getattr(self, "_checkin_flow_locks", None)
        if locks is None:
            locks = weakref.WeakValueDictionary()
            self._checkin_flow_locks = locks
        lock = locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[user_id] = lock
        return lock

    # ──────────────────────────────────────────────────────────────
    # 配置读取（带类型校验）
    # 正式配置按六个分组加载；读取层保留简单 dict 支持，不承担旧配置迁移。
    # ──────────────────────────────────────────────────────────────

    _CONFIG_GROUP_KEYS = (
        "pixiv_source",
        "content_dedupe",
        "checkin_omnidraw",
        "checkin_basic",
        "checkin_shop",
        "runtime",
    )

    def _cfg_get(self, key: str, default=None):
        config = getattr(self, "config", None)
        if config is None:
            return default
        for group_key in self._CONFIG_GROUP_KEYS:
            try:
                group = config.get(group_key)
            except Exception:
                group = None
            if group is None:
                continue
            try:
                if key in group:
                    return group[key]
            except Exception:
                pass
        try:
            return config.get(key, default)
        except Exception:
            return default

    def _cfg_contains(self, key: str) -> bool:
        config = getattr(self, "config", None)
        if config is None:
            return False
        for group_key in self._CONFIG_GROUP_KEYS:
            try:
                group = config.get(group_key)
            except Exception:
                group = None
            if group is None:
                continue
            try:
                if key in group:
                    return True
            except Exception:
                pass
        try:
            return key in config
        except Exception:
            return False

    def _cleanup_retired_transport_config(self) -> None:
        """清理已停用的传输开关；旧扁平配置迁移已结束。"""
        config = getattr(self, "config", None)
        if config is None:
            return
        # 旧传输开关统一迁移到自动策略，保留保存失败时的原值供下次重试。
        retired_key = "onebot_image_transport"
        containers = [config]
        if isinstance(config.get("pixiv_source"), dict):
            containers.append(config["pixiv_source"])
        removed = [(container, container.pop(retired_key))
                   for container in containers if retired_key in container]
        if removed:
            try:
                saver = getattr(config, "save_config", None)
                if callable(saver):
                    saver()
                logger.info(f"{LOG_PREFIX} 图片传输配置迁移完成: transport=auto removed_count={len(removed)}")
            except Exception as exc:
                for container, value in removed:
                    container[retired_key] = value
                logger.warning(f"{LOG_PREFIX} 图片传输配置迁移保存失败: error_type={type(exc).__name__}")

    def _cfg_str(self, key: str, default: str = "") -> str:
        val = self._cfg_get(key, default)
        return str(val).strip() if val is not None else default

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        raw = self._cfg_get(key, default)
        if isinstance(raw, (bool, float)):
            return default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    def _forward_threshold(self) -> int:
        """Return the merged-forward threshold, accepting the retired bool setting."""
        if self._cfg_contains("forward_threshold"):
            return self._cfg_int("forward_threshold", 1, 0, MAX_IMAGE_COUNT)
        return 0 if self._cfg_bool("send_as_forward", True) else MAX_IMAGE_COUNT

    def _cfg_float(self, key: str, default: float, lo: float, hi: float) -> float:
        try:
            val = float(self._cfg_get(key, default))
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self._cfg_get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val) if val is not None else default
