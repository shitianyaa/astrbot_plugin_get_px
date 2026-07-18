"""AstrBot 插件 — 安全插画发图与签到

通过标签搜索插画并发送图片，支持 Lolicon 主源、Pixiv 回退、内容安全过滤、多页作品、自然语言自动触发和签到。

搜索指令：
    /p [标签] [数量]           搜索并发送图片

自动触发（需在配置中开启）：
    来一份图                   发送 1 张随机图片
    来三张初音ミク图             搜索标签「初音ミク」发送 3 张

签到指令：
    /签到                      每日签到
    /签到中心                  查看签到功能分组
    /签到帮助                  发送签到中心帮助图
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

from astrbot.api.all import AstrBotConfig, Image, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.star.star_tools import StarTools
from .checkin import CheckinStore, UnversionedCheckinDatabaseError
from .checkin.application import CheckinApplicationMixin
from .checkin.artwork import CheckinArtworkMixin
from .checkin.cache import CheckinCardCache
from .checkin.commands import CheckinCommandMixin
from .checkin.greeting import CheckinGreetingGenerator
from .checkin.holiday import HolidayCalendar
from .checkin.shop import CheckinShopMixin
from .pixiv import DeliveryMixin, FiltersMixin, SearchMixin
from .pixiv.client import PixivClient
from .pixiv.downloader import ImageDownloader
from .pixiv.index import ImageIndexStore
from .pixiv.lolicon import LoliconClient
from .plugin_api import PluginWebApi

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
PLUGIN_VERSION = "v3.3.1"
WEB_INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试"

AUTO_TRIGGER_PATTERN = r"^/?(来\s*(.*?)(份|个|张|点))(.*?)(福利|色|瑟|涩|塞)?图$"
CHECKIN_REGEX_PATTERN = r"^(?!/)签到$"
CHECKIN_CENTER_HELP_IMAGE = (
    Path(__file__).resolve().parent / "assets" / "checkin_center_help_v3.png"
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
        self.client: PixivClient | None = None
        self.lolicon_client: LoliconClient | None = None
        self.downloader = ImageDownloader()
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
        self._init_client()
        # SQLite DDL/迁移是同步操作，放入线程池避免阻塞事件循环
        self.image_index = await asyncio.to_thread(ImageIndexStore, data_dir)
        await self.image_index.cleanup_old_days()
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
        self.checkin_cache = CheckinCardCache(self.data_dir / "checkin_card_cache")
        await asyncio.to_thread(self.checkin_cache.cleanup_expired, force=True)
        self.holiday_calendar = HolidayCalendar(
            self.data_dir,
            plugin_version=PLUGIN_VERSION,
        )
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
            logger.warning(f"{LOG_PREFIX} 节假日数据更新失败，继续使用本地规则: {exc}")

    def _init_client(self):
        """初始化 Lolicon 主源和可选的 Pixiv 回退客户端。"""
        lolicon_url = self._cfg_str(
            "lolicon_api_url", "https://api.lolicon.app/setu/v2"
        )
        if getattr(self, "lolicon_client", None) is None:
            self.lolicon_client = LoliconClient(
                api_url=lolicon_url,
                exclude_ai=self._cfg_bool("lolicon_exclude_ai", True),
                request_timeout=self._cfg_float(
                    "request_timeout", 30.0, 5.0, 120.0
                ),
            )
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.info(f"{LOG_PREFIX} 未配置 Pixiv refresh_token，仅使用 Lolicon 主源")
            return

        self.client = PixivClient(
            refresh_token=token,
            request_timeout=self._cfg_float("request_timeout", 30.0, 5.0, 120.0),
        )
        logger.info(f"{LOG_PREFIX} Lolicon 主源和 Pixiv 回退客户端已初始化")

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
        if self._holiday_refresh_task is not None:
            self._holiday_refresh_task.cancel()
            await asyncio.gather(self._holiday_refresh_task, return_exceptions=True)
            self._holiday_refresh_task = None
        if getattr(self, "client", None) is not None:
            try:
                await self.client.close()
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 关闭 Pixiv 客户端失败: {exc}")
            finally:
                self.client = None
        if getattr(self, "lolicon_client", None) is not None:
            try:
                await self.lolicon_client.close()
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 关闭 Lolicon 客户端失败: {exc}")
            finally:
                self.lolicon_client = None
        try:
            await self.downloader.close()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 关闭图片下载器失败: {exc}")
        try:
            await self.checkin_greeting.close()
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 关闭签到问候会话失败: {exc}")
        self._last_request.clear()
        locks = getattr(self, "_checkin_flow_locks", None)
        if locks is not None:
            locks.clear()
        if self.image_index is not None:
            try:
                self.image_index.close()
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 关闭图片索引失败: {exc}")
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
        """发送签到中心功能帮助图。"""
        event.stop_event()
        if not CHECKIN_CENTER_HELP_IMAGE.is_file():
            logger.error(
                f"{LOG_PREFIX} 签到中心帮助图片不存在: {CHECKIN_CENTER_HELP_IMAGE}"
            )
            yield event.plain_result("签到中心帮助图片缺失，请联系管理员重新安装插件")
            return
        yield event.chain_result(
            [Image.fromFileSystem(str(CHECKIN_CENTER_HELP_IMAGE))]
        )

    @filter.command_group("签到中心")
    def checkin_center(self):
        """签到功能中心。"""

    @checkin_center.group("我的")
    def checkin_personal(self):
        """个人签到资料。"""

    @checkin_personal.command("状态")
    async def cmd_checkin_status(self, event: AstrMessageEvent):
        """查看金币、好感度和连续签到状态。"""
        event.stop_event()
        async for result in self._handle_checkin_status(event):
            yield result

    @checkin_personal.command("生日")
    async def cmd_checkin_birthday(
        self, event: AstrMessageEvent, action: str = "", value: str = ""
    ):
        """查看、设置或清除签到生日。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_birthday(event, action, value)
        )

    @checkin_personal.command("成就")
    async def cmd_checkin_achievements(self, event: AstrMessageEvent):
        """查看签到成就。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_achievements(event))

    @checkin_personal.group("称号")
    def checkin_titles(self):
        """查看和佩戴签到称号。"""

    @checkin_titles.command("查看")
    async def cmd_checkin_titles(self, event: AstrMessageEvent):
        """查看已解锁的签到称号。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_titles(event))

    @checkin_titles.command("佩戴")
    async def cmd_select_checkin_title(self, event: AstrMessageEvent, title: str = ""):
        """佩戴已解锁的签到称号。"""
        event.stop_event()
        yield event.plain_result(await self._handle_select_checkin_title(event, title))

    @checkin_center.command("排行")
    async def cmd_checkin_ranking(self, event: AstrMessageEvent, mode: str = ""):
        """查看当前群的签到排行。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_ranking(event, mode))

    @filter.regex(CHECKIN_REGEX_PATTERN)
    async def checkin_auto_trigger(self, event: AstrMessageEvent):
        """纯文本触发签到。"""
        if not self._cfg_bool("checkin_enabled", True):
            return
        event.stop_event()
        async for result in self._handle_checkin(event, silent_when_disabled=True):
            yield result

    @checkin_center.group("商店")
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

    @checkin_shop.group("主题")
    def checkin_themes(self):
        """签到主题商店。"""

    @checkin_themes.command("列表")
    async def cmd_checkin_themes(self, event: AstrMessageEvent):
        """查看已购买和可购买的签到主题。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_themes(event))

    @checkin_themes.command("查看")
    async def cmd_preview_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """查看指定签到主题的静态预览图。"""
        event.stop_event()
        yield await self._handle_checkin_theme_preview(event, theme)

    @checkin_themes.command("购买")
    async def cmd_buy_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """购买签到主题，购买成功后自动切换。"""
        event.stop_event()
        yield event.plain_result(await self._handle_buy_checkin_theme(event, theme))

    @checkin_themes.command("切换")
    async def cmd_select_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """切换到默认或已购买的签到主题。"""
        event.stop_event()
        yield event.plain_result(await self._handle_select_checkin_theme(event, theme))

    @checkin_shop.command("刷新背景")
    async def cmd_refresh_checkin_background(self, event: AstrMessageEvent):
        """花费金币重新抽取今天的签到背景。"""
        event.stop_event()
        async for result in self._handle_refresh_checkin_background(event):
            yield result

    @checkin_center.group("管理")
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
    @checkin_admin.command("事件")
    async def cmd_checkin_event_admin(
        self,
        event: AstrMessageEvent,
        action: str = "",
        event_type: str = "",
        date_value: str = "",
        name: str = "",
    ):
        """管理员维护全局签到纪念日。"""
        event.stop_event()
        parts = event.get_message_str().strip().split(maxsplit=6)
        if parts[:3] == ["签到中心", "管理", "事件"]:
            action = parts[3] if len(parts) > 3 else action
            event_type = parts[4] if len(parts) > 4 else event_type
            date_value = parts[5] if len(parts) > 5 else date_value
            name = parts[6] if len(parts) > 6 else name
        yield event.plain_result(
            await self._handle_checkin_event_admin(
                event, action, event_type, date_value, name
            )
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

        logger.info(f"{LOG_PREFIX} 自然语言触发: count={count_str} tag={tag_part!r}")
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
    # ──────────────────────────────────────────────────────────────

    def _cfg_str(self, key: str, default: str = "") -> str:
        val = self.config.get(key, default)
        return str(val).strip() if val is not None else default

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        raw = self.config.get(key, default)
        if isinstance(raw, (bool, float)):
            return default
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    def _cfg_float(self, key: str, default: float, lo: float, hi: float) -> float:
        try:
            val = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return val if lo <= val <= hi else default

    def _cfg_bool(self, key: str, default: bool) -> bool:
        val = self.config.get(key, default)
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return bool(val) if val is not None else default
