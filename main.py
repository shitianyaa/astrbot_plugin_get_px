"""AstrBot 插件 — Pixiv 发图

通过标签搜索 Pixiv 插画并发送图片，支持排行榜、内容安全过滤、多页作品、代理配置、自然语言自动触发和签到。

搜索指令：
    /p [标签] [数量]           搜索并发送图片

自动触发（需在配置中开启）：
    来一份图                   发送 1 张排行榜图片
    来三张初音ミク图             搜索标签「初音ミク」发送 3 张

管理指令：
    /pr [排行类型] [数量]      获取排行榜
    /prl                       查看所有排行榜类型
    /pi <作品ID>               查看作品详情
    /签到                      每日签到
    /签到测试                  管理员预览签到卡片
    /ph                        查看帮助
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import re
import time
import weakref

from astrbot.api.all import AstrBotConfig, Image, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools
from .checkin import CheckinStore
from .checkin.application import CheckinApplicationMixin
from .checkin.artwork import CheckinArtworkMixin
from .checkin.cache import CheckinCardCache
from .checkin.commands import CheckinCommandMixin
from .checkin.greeting import CheckinGreetingGenerator
from .checkin.holiday import HolidayCalendar
from .pixiv import DeliveryMixin, FiltersMixin, SearchMixin
from .pixiv.client import PixivClient
from .pixiv.downloader import ImageDownloader
from .pixiv.index import ImageIndexStore
from .plugin_api import PluginWebApi

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
PLUGIN_VERSION = "3.0.0"
WEB_INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试"

AUTO_TRIGGER_PATTERN = r"^/?(来\s*(.*?)(份|个|张|点))(.*?)(福利|色|瑟|涩|塞)?图$"
CHECKIN_REGEX_PATTERN = r"^(?!/)签到$"
CHECKIN_HELP_IMAGE = Path(__file__).resolve().parent / "assets" / "checkin_help.png"

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

# ──────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────


class GetPxPlugin(
    CheckinApplicationMixin,
    CheckinCommandMixin,
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
        self.image_index = ImageIndexStore(data_dir)
        await self.image_index.cleanup_old_days()
        self.checkin_store = CheckinStore(data_dir)
        self.checkin_cache = CheckinCardCache(self.data_dir / "checkin_card_cache")
        self.checkin_cache.cleanup_expired(force=True)
        self.holiday_calendar = HolidayCalendar(
            self.data_dir,
            plugin_version=PLUGIN_VERSION,
        )
        self._holiday_refresh_task = asyncio.create_task(
            self._refresh_holiday_calendar()
        )
        self.plugin_web_api.register()
        logger.info(f"{LOG_PREFIX} 插件已加载")

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
        """根据配置初始化 Pixiv 客户端。"""
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.warning(f"{LOG_PREFIX} 未配置 pixiv_refresh_token，插件将不可用")
            return

        proxy = self._cfg_str("pixiv_proxy_url")
        self.client = PixivClient(
            refresh_token=token,
            proxy=proxy,
            request_timeout=self._cfg_float("request_timeout", 30.0, 5.0, 120.0),
        )
        logger.info(f"{LOG_PREFIX} 客户端已初始化")

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
        """插件卸载/停用时清理资源。"""
        if self._holiday_refresh_task is not None:
            self._holiday_refresh_task.cancel()
            await asyncio.gather(self._holiday_refresh_task, return_exceptions=True)
            self._holiday_refresh_task = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        await self.downloader.close()
        self._last_request.clear()
        locks = getattr(self, "_checkin_flow_locks", None)
        if locks is not None:
            locks.clear()
        if self.image_index is not None:
            self.image_index.close()
        self.image_index = None
        self.checkin_store = None
        logger.info(f"{LOG_PREFIX} 插件已停止")

    # ──────────────────────────────────────────────────────────────
    # 指令：搜索（主指令）
    # ──────────────────────────────────────────────────────────────

    @filter.command("p")
    async def cmd_p(self, event: AstrMessageEvent, tag: str = "", count: str = ""):
        """搜索 Pixiv 并发送图片。用法: /p [标签] [数量]"""
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token"
            )
            return
        event.stop_event()
        # 如果 tag 是纯数字，视为数量（无标签搜索排行榜）
        if tag and tag.isdigit():
            async for result in self._handle_search(event, tag="", count_str=tag):
                yield result
        else:
            async for result in self._handle_search(event, tag=tag, count_str=count):
                yield result

    # ──────────────────────────────────────────────────────────────
    # 管理指令
    # ──────────────────────────────────────────────────────────────

    @filter.command("pr")
    async def cmd_rank(self, event: AstrMessageEvent, mode: str = "", count: str = ""):
        """获取 Pixiv 排行榜。用法: /pr [类型] [数量]"""
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token"
            )
            return
        event.stop_event()
        async for result in self._handle_rank(event, mode, count):
            yield result

    @filter.command("prl")
    async def cmd_rank_list(self, event: AstrMessageEvent):
        """查看所有 Pixiv 排行榜类型。"""
        event.stop_event()
        lines = ["📊 可用排行榜类型："]
        for k, v in RANKING_MODES.items():
            lines.append(f"  · {k} — {v}")
        lines.append("\n用法: /pr [类型] [数量]")
        yield event.plain_result("\n".join(lines))

    @filter.command("pi")
    async def cmd_info(self, event: AstrMessageEvent, illust_id: str = ""):
        """查看 Pixiv 作品详情。用法: /pi <作品ID>"""
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token"
            )
            return
        if not illust_id or not illust_id.isdigit():
            yield event.plain_result("⚠️ 用法: /pi <作品ID>\n示例: /pi 12345678")
            return
        event.stop_event()
        async for result in self._handle_info(event, int(illust_id)):
            yield result

    @filter.command("pd")
    async def cmd_download(self, event: AstrMessageEvent, illust_id: str = ""):
        """通过作品ID下载并发送图片。用法: /pd <作品ID> [页码]"""
        if not self._ensure_client_or_error(event):
            yield event.plain_result(
                "⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token"
            )
            return
        if not illust_id:
            yield event.plain_result(
                "⚠️ 用法: /pd <作品ID> [页码]\n示例: /pd 12345678\n多页作品可指定页码: /pd 12345678 2"
            )
            return
        event.stop_event()

        # 解析参数：/pd 12345678 或 /pd 12345678 2
        parts = illust_id.split()
        if len(parts) == 1:
            id_str = parts[0]
            page_str = "1"
        elif len(parts) == 2:
            id_str, page_str = parts
        else:
            yield event.plain_result(
                "⚠️ 用法: /pd <作品ID> [页码]\n示例: /pd 12345678\n多页作品可指定页码: /pd 12345678 2"
            )
            return

        if not id_str.isdigit():
            yield event.plain_result("⚠️ 作品ID必须是数字\n用法: /pd <作品ID> [页码]")
            return

        try:
            page = int(page_str) if page_str.isdigit() else 1
        except (TypeError, ValueError):
            page = 1

        async for result in self._handle_download(event, int(id_str), page):
            yield result

    @filter.command("ph")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 Pixiv 插件帮助。"""
        event.stop_event()
        yield event.plain_result(self._build_help())

    @filter.command("签到")
    async def cmd_checkin(self, event: AstrMessageEvent):
        """每日签到。"""
        event.stop_event()
        async for result in self._handle_checkin(event):
            yield result

    @filter.command("签到排行")
    async def cmd_checkin_ranking(self, event: AstrMessageEvent, mode: str = ""):
        """查看当前群的签到排行。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_ranking(event, mode))

    @filter.command("签到帮助")
    async def cmd_checkin_help(self, event: AstrMessageEvent):
        """发送签到功能帮助图片。"""
        event.stop_event()
        if not CHECKIN_HELP_IMAGE.is_file():
            logger.error(f"{LOG_PREFIX} 签到帮助图片不存在: {CHECKIN_HELP_IMAGE}")
            yield event.plain_result("签到帮助图片缺失，请联系管理员重新安装插件")
            return
        yield event.chain_result([Image.fromFileSystem(str(CHECKIN_HELP_IMAGE))])

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("签到测试")
    async def cmd_checkin_preview(self, event: AstrMessageEvent):
        """用真实用户资料和问候配置预览卡片，不写入签到数据。"""
        event.stop_event()
        async for result in self._handle_checkin_preview(event):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("签到导出")
    async def cmd_checkin_export(self, event: AstrMessageEvent):
        """管理员导出签到完整备份。"""
        event.stop_event()
        result = await self._handle_checkin_export(event)
        if result is not None:
            yield result

    @filter.regex(CHECKIN_REGEX_PATTERN)
    async def checkin_auto_trigger(self, event: AstrMessageEvent):
        """纯文本触发签到。"""
        if not self._cfg_bool("checkin_enabled", True):
            return
        event.stop_event()
        async for result in self._handle_checkin(event, silent_when_disabled=True):
            yield result

    @filter.command("签到状态")
    async def cmd_checkin_status(self, event: AstrMessageEvent):
        """查看签到状态。"""
        event.stop_event()
        async for result in self._handle_checkin_status(event):
            yield result

    @filter.command("签到商店")
    async def cmd_checkin_shop(self, event: AstrMessageEvent):
        """查看签到商店。"""
        event.stop_event()
        if not self._cfg_bool("checkin_enabled", True):
            yield event.plain_result("签到功能已关闭")
            return
        yield event.plain_result(self._build_checkin_shop())

    @filter.command("购买加持")
    async def cmd_buy_checkin_boost(self, event: AstrMessageEvent, days: str = ""):
        """购买好感度双倍加持。"""
        event.stop_event()
        async for result in self._handle_buy_checkin_boost(event, days):
            yield result

    @filter.command("签到主题")
    async def cmd_checkin_themes(self, event: AstrMessageEvent):
        """查看已购买和可购买的签到主题。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_themes(event))

    @filter.command("查看主题")
    async def cmd_preview_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """查看指定签到主题的静态预览图。"""
        event.stop_event()
        yield await self._handle_checkin_theme_preview(event, theme)

    @filter.command("购买主题")
    async def cmd_buy_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """购买签到主题，购买成功后自动切换。"""
        event.stop_event()
        yield event.plain_result(await self._handle_buy_checkin_theme(event, theme))

    @filter.command("切换主题")
    async def cmd_select_checkin_theme(self, event: AstrMessageEvent, theme: str = ""):
        """切换到默认或已购买的签到主题。"""
        event.stop_event()
        yield event.plain_result(await self._handle_select_checkin_theme(event, theme))

    @filter.command("刷新签到背景")
    async def cmd_refresh_checkin_background(self, event: AstrMessageEvent):
        """花费金币重新抽取今天的签到背景。"""
        event.stop_event()
        async for result in self._handle_refresh_checkin_background(event):
            yield result

    @filter.command("签到生日")
    async def cmd_checkin_birthday(
        self, event: AstrMessageEvent, action: str = "", value: str = ""
    ):
        """查看或自动读取签到生日，也可手动设置或清除。"""
        event.stop_event()
        yield event.plain_result(
            await self._handle_checkin_birthday(event, action, value)
        )

    @filter.command("签到成就")
    async def cmd_checkin_achievements(self, event: AstrMessageEvent):
        """查看签到成就。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_achievements(event))

    @filter.command("签到称号")
    async def cmd_checkin_titles(self, event: AstrMessageEvent):
        """查看签到称号。"""
        event.stop_event()
        yield event.plain_result(await self._handle_checkin_titles(event))

    @filter.command("佩戴称号")
    async def cmd_select_checkin_title(self, event: AstrMessageEvent, title: str = ""):
        """佩戴已解锁的签到称号。"""
        event.stop_event()
        yield event.plain_result(await self._handle_select_checkin_title(event, title))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("签到事件")
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
        raw = event.get_message_str().strip().lstrip("/")
        parts = raw.split(maxsplit=4)
        if parts and parts[0] == "签到事件":
            action = parts[1] if len(parts) > 1 else action
            event_type = parts[2] if len(parts) > 2 else event_type
            date_value = parts[3] if len(parts) > 3 else date_value
            name = parts[4] if len(parts) > 4 else name
        yield event.plain_result(
            await self._handle_checkin_event_admin(
                event, action, event_type, date_value, name
            )
        )

    @filter.regex(AUTO_TRIGGER_PATTERN)
    async def auto_trigger(self, event: AstrMessageEvent):
        """自然语言自动触发 Pixiv 发图。"""
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
    # 子指令实现
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _build_help() -> str:
        lines = [
            "📖 Pixiv 发图 — 使用帮助",
            "",
            "🔍 /p [标签] [数量]",
            "　　按标签搜索并发送图片（默认 1 张）",
            "　　示例: /p 初音ミク 3",
            "",
            "📊 /pr [排行类型] [数量]",
            "　　获取排行榜",
            "　　发送 /prl 查看所有类型",
            "",
            "🔎 /pi <作品ID>",
            "　　查看作品详情",
            "　　示例: /pi 12345678",
            "",
            "📥 /pd <作品ID> [页码]",
            "　　通过作品ID下载并发送图片",
            "　　多页作品可指定页码",
            "　　示例: /pd 12345678",
            "　　示例: /pd 12345678 2",
            "",
            "签到",
            "　　每日签到，也可直接发送 签到",
            "",
            "签到帮助",
            "　　发送完整签到功能帮助图片",
            "",
            "签到测试",
            "　　管理员预览签到卡片，不写入签到数据",
            "",
            "签到导出",
            "　　管理员导出签到完整备份文件",
            "",
            "签到状态",
            "　　查看累计签到、金币、好感度和加持状态",
            "",
            "签到排行 [今日|月榜|连签|累计]",
            "　　查看当前群独立统计的签到排行",
            "",
            "签到商店 / 购买加持 / 刷新签到背景",
            "　　使用金币购买好感度加持或重新抽取当日背景",
            "",
            "签到主题 / 查看主题 <编号>",
            "　　查看主题列表或直接预览指定主题",
            "购买主题 <编号> / 切换主题 <编号>",
            "　　解锁并切换签到卡片主题",
            "",
            "签到生日 / 签到成就 / 签到称号 / 佩戴称号",
            "　　查看或自动读取生日，查看成就并切换卡片称号",
            "",
            "签到事件（管理员）",
            "　　添加、查看或删除全局年度/单次纪念日",
            "",
            "🤖 自然语言触发（需配置开启 auto_trigger_enabled）",
            "　　来一份图 → 发送 1 张排行榜图片",
            "　　来三张初音ミク图 → 搜标签发送 3 张",
            "　　来两张萝莉图 → 搜标签发送 2 张",
            "",
            "⚙️ 漫画过滤（filter_manga）:",
            "　　开启后，只要作品类型为漫画就自动过滤",
            "　　漫画日榜 day_manga 作为主动请求保留后门",
            "",
            "❓ /ph 显示本帮助",
        ]
        return "\n".join(lines)

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
        try:
            val = int(self.config.get(key, default))
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
