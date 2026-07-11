"""AstrBot 插件 — Pixiv 发图

通过标签搜索 Pixiv 插画并发送图片，支持排行榜、R18 过滤、多页作品、代理配置、自然语言自动触发和签到。

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
import base64
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import random
import re
import time

from astrbot.api.all import AstrBotConfig, File, Image, Plain, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes
from astrbot.api.star import Context, Star
from astrbot.core.star.star_tools import StarTools
from quart import jsonify, request, send_file

from .ai_commenter import AiCommenter
from .checkin import (
    BOOST_PRODUCTS,
    CheckinProfile,
    CheckinRecord,
    CheckinResult,
    CheckinStore,
    affection_level,
    boost_status_text,
    dump_checkin_snapshot_json,
    load_checkin_snapshot_json,
)
from .checkin_background import (
    CHECKIN_ARTWORK_TARGET_RATIO,
    CHECKIN_ARTWORK_TOLERANCE,
    filter_illusts_by_aspect_ratio,
)
from .checkin_cache import CheckinCardCache
from .checkin_card import (
    CHECKIN_CARD_HEIGHT,
    CHECKIN_CARD_TEMPLATE,
    CHECKIN_CARD_WIDTH,
    CardBackground,
    build_checkin_card_data,
)
from .checkin_content import resolve_checkin_content
from .checkin_greeting import (
    DEFAULT_CHECKIN_GREETING_PROMPT,
    CheckinGreetingGenerator,
)
from .downloader import ImageDownloader, cleanup, pick_image_url, pick_image_url_exact
from .image_index import ImageIndexStore, ordered_by_unused
from .image_history import DEFAULT_HISTORY_LIMIT, ImageAssetManager
from .pixiv_client import PixivClient

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"
PLUGIN_NAME = "astrbot_plugin_get_px"
WEB_INTERNAL_ERROR_MESSAGE = "服务内部错误，请稍后重试"

AUTO_TRIGGER_PATTERN = r"^/?(来\s*(.*?)(份|个|张|点))(.*?)(福利|色|瑟|涩|塞)?图$"
CHECKIN_REGEX_PATTERN = r"^(?!/)签到$"

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

DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0
DEFAULT_BLACKLIST_TAGS = "furry,裸体,全裸,触手,露出,nsfw"
THUMB_DATA_BATCH_LIMIT = 32
CHECKIN_BACKGROUND_PAGE_ATTEMPTS = 5


# ──────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────


class GetPxPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.client: PixivClient | None = None
        self.downloader = ImageDownloader()
        self.ai = AiCommenter(context)
        self._last_request: dict[str, float] = {}
        self.data_dir: Path | None = None
        self.image_index: ImageIndexStore | None = None
        self.image_history: ImageAssetManager | None = None
        self.checkin_store: CheckinStore | None = None
        self.checkin_cache: CheckinCardCache | None = None
        self.checkin_greeting = CheckinGreetingGenerator(context)
        self._checkin_flow_locks: dict[str, asyncio.Lock] = {}

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────

    async def initialize(self):
        """插件加载时初始化 Pixiv 客户端。"""
        self._init_client()
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.data_dir = Path(data_dir)
        self.image_index = ImageIndexStore(data_dir)
        await self.image_index.cleanup_old_days()
        self.image_history = ImageAssetManager(data_dir)
        self.checkin_store = CheckinStore(data_dir)
        self.checkin_cache = CheckinCardCache(
            self.data_dir / "checkin_card_cache"
        )
        self.checkin_cache.cleanup_expired(force=True)
        self._register_image_history_web_apis()
        logger.info(f"{LOG_PREFIX} 插件已加载")

    def _init_client(self):
        """根据配置初始化 Pixiv 客户端。"""
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.warning(f"{LOG_PREFIX} 未配置 pixiv_refresh_token，插件将不可用")
            return

        proxy = self._cfg_str("pixiv_proxy_url")
        self.client = PixivClient(refresh_token=token, proxy=proxy)
        logger.info(f"{LOG_PREFIX} 客户端已初始化")

    def _register_image_history_web_apis(self) -> None:
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history",
            self._web_image_history,
            ["GET"],
            "List sent Pixiv image history",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history/thumb",
            self._web_image_history_thumb,
            ["GET"],
            "Get image history thumbnail",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history/thumb-data",
            self._web_image_history_thumb_data,
            ["GET"],
            "Get image history thumbnail as data URL",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history/thumb-data-batch",
            self._web_image_history_thumb_data_batch,
            ["POST"],
            "Get image history thumbnails as data URLs",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history/clear",
            self._web_image_history_clear,
            ["POST"],
            "Clear sent Pixiv image history",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history/delete",
            self._web_image_history_delete,
            ["POST"],
            "Delete one sent Pixiv image history record",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-history/blacklist",
            self._web_image_history_blacklist,
            ["POST"],
            "Blacklist one Pixiv illustration from image history",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-blacklist",
            self._web_image_blacklist,
            ["GET"],
            "List blacklisted Pixiv illustrations",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-blacklist/thumb-data",
            self._web_image_blacklist_thumb_data,
            ["GET"],
            "Get blacklisted Pixiv illustration thumbnail as data URL",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-blacklist/thumb-data-batch",
            self._web_image_blacklist_thumb_data_batch,
            ["POST"],
            "Get blacklisted Pixiv illustration thumbnails as data URLs",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/image-blacklist/remove",
            self._web_image_blacklist_remove,
            ["POST"],
            "Remove one Pixiv illustration from blacklist",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/config",
            self._web_config,
            ["GET"],
            "Get plugin web UI configuration",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/checkin-import",
            self._web_checkin_import,
            ["POST"],
            "Import checkin backup snapshot",
        )

    def _web_internal_error(self, action: str, exc: Exception):
        logger.error(f"{LOG_PREFIX} Web API {action} 失败: {exc}")
        return jsonify({"success": False, "error": WEB_INTERNAL_ERROR_MESSAGE}), 500

    async def _web_image_history(self):
        if self.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        try:
            records = await self.image_history.list_records()
            return jsonify(
                {
                    "success": True,
                    "records": records,
                    "limit": DEFAULT_HISTORY_LIMIT,
                }
            )
        except Exception as e:
            return self._web_internal_error("读取图片历史", e)

    async def _web_image_history_thumb(self):
        if self.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        record_id = request.args.get("id", "").strip()
        path = await self.image_history.get_thumbnail_path(record_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            return await send_file(str(path), mimetype="image/jpeg")
        except Exception as e:
            return self._web_internal_error("读取图片历史缩略图", e)

    async def _web_image_history_thumb_data(self):
        if self.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        record_id = request.args.get("id", "").strip()
        path = await self.image_history.get_thumbnail_path(record_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as e:
            return self._web_internal_error("读取图片历史缩略图数据", e)
        return jsonify(
            {
                "success": True,
                "record_id": record_id,
                "data_url": f"data:image/jpeg;base64,{encoded}",
            }
        )

    async def _web_image_history_thumb_data_batch(self):
        if self.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        ids = self._normalize_request_ids(payload.get("ids"))
        if not ids:
            return jsonify({"success": True, "thumbs": {}, "missing": []})
        paths = await self.image_history.get_thumbnail_paths(ids)
        try:
            thumbs = await asyncio.to_thread(self._encode_thumb_data_urls, paths)
        except OSError as e:
            return self._web_internal_error("批量读取图片历史缩略图数据", e)
        return jsonify(
            {
                "success": True,
                "thumbs": thumbs,
                "missing": [record_id for record_id in ids if record_id not in thumbs],
            }
        )

    async def _web_image_history_clear(self):
        if self.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        try:
            deleted = await self.image_history.clear()
            logger.info(f"{LOG_PREFIX} WebUI 已清空图片历史: deleted={deleted}")
            return jsonify({"success": True, "deleted": deleted})
        except Exception as e:
            return self._web_internal_error("清空图片历史", e)

    async def _web_image_history_delete(self):
        if self.image_history is None:
            return jsonify({"success": False, "error": "图片历史尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        record_id = str(payload.get("record_id") or "").strip()
        if not record_id:
            return jsonify({"success": False, "error": "record_id is required"}), 400
        try:
            deleted = await self.image_history.delete_record(record_id)
            if deleted is None:
                return jsonify({"success": False, "error": "记录不存在"}), 404
            logger.info(
                f"{LOG_PREFIX} WebUI 已删除图片历史记录: "
                f"record_id={record_id}, illust_id={deleted.get('illust_id', '')}"
            )
            return jsonify({"success": True, "record": deleted})
        except Exception as e:
            return self._web_internal_error("删除图片历史记录", e)

    async def _web_image_history_blacklist(self):
        if self.image_history is None or self.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        record_id = str(payload.get("record_id") or "").strip()
        illust_id = str(payload.get("illust_id") or "").strip()
        record = await self.image_history.get_record(record_id) if record_id else None
        if record is not None:
            illust_id = str(record.get("illust_id") or illust_id).strip()
        if not illust_id:
            return jsonify({"success": False, "error": "illust_id is required"}), 400
        try:
            thumb_path = (
                await self.image_history.get_thumbnail_path(record_id)
                if record_id
                else None
            )
            thumb_id = await self.image_index.save_blacklist_thumbnail(
                illust_id=illust_id,
                source_path=thumb_path,
            )
            await self.image_index.add_blacklist_illust(
                illust_id=illust_id,
                title=str((record or {}).get("title") or payload.get("title") or ""),
                author=str((record or {}).get("author") or payload.get("author") or ""),
                source=str((record or {}).get("source") or payload.get("source") or ""),
                record_id=record_id,
                thumb_id=thumb_id,
            )
            deleted = await self.image_history.delete_records_by_illust_id(illust_id)
            blacklist_record = None
            for item in await self.image_index.list_blacklist_illusts():
                if str(item.get("illust_id") or "") == illust_id:
                    blacklist_record = item
                    break
            logger.info(
                f"{LOG_PREFIX} WebUI 已加入图片黑名单并清理历史记录: "
                f"illust_id={illust_id}, deleted={len(deleted)}, thumb_id={thumb_id}"
            )
            return jsonify(
                {
                    "success": True,
                    "illust_id": illust_id,
                    "deleted": len(deleted),
                    "record": blacklist_record,
                }
            )
        except Exception as e:
            return self._web_internal_error("加入作品黑名单", e)

    async def _web_image_blacklist(self):
        if self.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        try:
            records = await self.image_index.list_blacklist_illusts()
            return jsonify({"success": True, "records": records})
        except Exception as e:
            return self._web_internal_error("读取作品黑名单", e)

    async def _web_image_blacklist_thumb_data(self):
        if self.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        illust_id = request.args.get("id", "").strip()
        path = await self.image_index.get_blacklist_thumbnail_path(illust_id)
        if path is None:
            return jsonify({"success": False, "error": "缩略图不存在"}), 404
        try:
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        except OSError as e:
            return self._web_internal_error("读取黑名单缩略图数据", e)
        return jsonify(
            {
                "success": True,
                "illust_id": illust_id,
                "data_url": f"data:image/jpeg;base64,{encoded}",
            }
        )

    async def _web_image_blacklist_thumb_data_batch(self):
        if self.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        ids = self._normalize_request_ids(payload.get("ids"))
        if not ids:
            return jsonify({"success": True, "thumbs": {}, "missing": []})
        paths = await self.image_index.get_blacklist_thumbnail_paths(ids)
        try:
            thumbs = await asyncio.to_thread(self._encode_thumb_data_urls, paths)
        except OSError as e:
            return self._web_internal_error("批量读取黑名单缩略图数据", e)
        return jsonify(
            {
                "success": True,
                "thumbs": thumbs,
                "missing": [illust_id for illust_id in ids if illust_id not in thumbs],
            }
        )

    async def _web_image_blacklist_remove(self):
        if self.image_index is None:
            return jsonify({"success": False, "error": "图片索引尚未初始化"}), 503
        payload = await request.get_json(silent=True) or {}
        illust_id = str(payload.get("illust_id") or "").strip()
        if not illust_id:
            return jsonify({"success": False, "error": "illust_id is required"}), 400
        try:
            removed = await self.image_index.remove_blacklist_illust(illust_id)
            if removed is None:
                return jsonify({"success": False, "error": "黑名单记录不存在"}), 404
            return jsonify({"success": True, "record": removed})
        except Exception as e:
            return self._web_internal_error("移出作品黑名单", e)

    async def _web_config(self):
        """Return plugin configuration for the web UI."""
        font_source = self._cfg_str("webui_font_source", "mirror")
        font_urls = {
            "mirror": "https://fonts.googleapis.cn/css2?family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@400;500;700&display=swap",
            "official": "https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;700&family=Noto+Serif+SC:wght@400;500;700&display=swap",
        }
        return jsonify(
            {
                "success": True,
                "font_source": font_source,
                "font_url": font_urls.get(font_source, ""),
            }
        )

    async def _web_checkin_import(self):
        if self.checkin_store is None:
            return jsonify({"success": False, "error": "签到数据尚未初始化"}), 503
        try:
            files = await request.files
            uploaded = []
            for key in files.keys():
                uploaded.extend(files.getlist(key))
            if not uploaded:
                return jsonify({"success": False, "error": "缺少备份文件"}), 400
            if len(uploaded) != 1:
                return jsonify({"success": False, "error": "一次只能上传 1 个备份文件"}), 400

            upload = uploaded[0]
            filename = str(getattr(upload, "filename", "") or "").strip()
            if not filename:
                return jsonify({"success": False, "error": "备份文件名不能为空"}), 400
            if not filename.lower().endswith(".json"):
                return jsonify({"success": False, "error": "只支持导入 JSON 备份文件"}), 400

            raw = await self._read_uploaded_file_bytes(upload)
            snapshot = load_checkin_snapshot_json(raw)
            rollback_path = await self._write_checkin_snapshot_backup(
                prefix="checkin-import-backup"
            )
            result = await self.checkin_store.import_snapshot(snapshot)
            return jsonify(
                {
                    "success": True,
                    "filename": filename,
                    "profiles": result["profiles"],
                    "records": result["records"],
                    "exported_at": result["exported_at"],
                    "imported_at": result["imported_at"],
                    "rollback_path": str(rollback_path),
                }
            )
        except ValueError as e:
            return jsonify({"success": False, "error": str(e)}), 400
        except Exception as e:
            return self._web_internal_error("导入签到备份", e)

    async def terminate(self):
        """插件卸载/停用时清理资源。"""
        if self.client is not None:
            await self.client.close()
            self.client = None
        await self.downloader.close()
        self._last_request.clear()
        self.image_index = None
        self.image_history = None
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("签到测试")
    async def cmd_checkin_preview(self, event: AstrMessageEvent):
        """管理员预览签到卡片，不写入签到数据。"""
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

    def _ensure_client_or_error(self, event: AstrMessageEvent) -> bool:
        """确保 Pixiv 客户端可用。返回 True 表示可用，False 表示不可用（已发送错误消息）。"""
        if self.client and self.client.api:
            return True
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            return False
        self._init_client()
        return self.client is not None

    @staticmethod
    def _normalize_request_ids(value) -> list[str]:
        raw_ids = value if isinstance(value, list) else []
        ids: list[str] = []
        seen: set[str] = set()
        for raw_id in raw_ids:
            item = str(raw_id or "").strip()
            if not item or item in seen:
                continue
            seen.add(item)
            ids.append(item)
            if len(ids) >= THUMB_DATA_BATCH_LIMIT:
                break
        return ids

    @staticmethod
    def _encode_thumb_data_urls(paths: dict[str, object]) -> dict[str, str]:
        thumbs: dict[str, str] = {}
        for item_id, path in paths.items():
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            thumbs[item_id] = f"data:image/jpeg;base64,{encoded}"
        return thumbs

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

    @staticmethod
    def _checkin_backup_name(prefix: str) -> str:
        return f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}.json"

    async def _write_checkin_snapshot_backup(self, *, prefix: str) -> Path:
        if self.checkin_store is None:
            raise RuntimeError("签到数据尚未初始化")
        snapshot = await self.checkin_store.export_snapshot()
        return self._write_checkin_snapshot_file(snapshot, prefix=prefix)

    def _write_checkin_snapshot_file(
        self, snapshot: dict[str, object], *, prefix: str
    ) -> Path:
        backup_dir = self._checkin_backup_dir()
        file_path = backup_dir / self._checkin_backup_name(prefix)
        index = 1
        while file_path.exists():
            file_path = backup_dir / f"{prefix}-{time.strftime('%Y%m%d-%H%M%S')}-{index}.json"
            index += 1
        payload = dump_checkin_snapshot_json(snapshot)
        file_path.write_text(payload, encoding="utf-8")
        return file_path

    async def _read_uploaded_file_bytes(self, upload) -> bytes:
        filename = str(getattr(upload, "filename", "") or "").strip() or "upload.json"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)
        temp_path = self._checkin_backup_dir() / f".upload-{time.time_ns()}-{safe_name}"
        await upload.save(str(temp_path))
        try:
            return temp_path.read_bytes()
        finally:
            cleanup(str(temp_path))

    async def _record_sent_image(
        self,
        event: AstrMessageEvent,
        illust: dict,
        image_path: str,
        *,
        source: str,
        page: int = 1,
        quality: str = "",
        file_size: int = 0,
    ) -> None:
        if self.image_history is None:
            return
        try:
            await self.image_history.record_sent(
                illust=illust,
                image_path=image_path,
                event=event,
                source=source,
                page=page,
                quality=quality,
                file_size=file_size,
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 写入图片历史失败: {e}")

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

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id() or ''}"

    @staticmethod
    def _source_key(tag: str, ranking_mode: str) -> str:
        return f"search:{tag.strip().casefold()}" if tag else f"rank:{ranking_mode}"

    async def _fetch_paginated(
        self,
        event: AstrMessageEvent,
        tag: str | None,
        ranking_mode: str,
    ) -> tuple[list[dict], int, str]:
        """带分页游标的 Pixiv 搜索/排行获取。

        Returns:
            (illusts, raw_count, source_key) — raw_count 为过滤前原始数量。
        """
        source_key = self._source_key(tag or "", ranking_mode)
        page_offset = 0
        if self.image_index is not None:
            try:
                page_offset = await self.image_index.get_page_offset(
                    self._event_scope(event), source_key
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 读取分页游标失败: {e}")

        try:
            if tag:
                illusts = await self.client.search(tag, offset=page_offset)
            else:
                illusts = await self.client.ranking(ranking_mode, offset=page_offset)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} Pixiv 请求失败(tag={tag!r}): {e}")
            return [], 0, source_key

        raw_count = len(illusts)
        return illusts, raw_count, source_key

    async def _record_image_usage(
        self,
        event: AstrMessageEvent,
        source_key: str,
        illust: dict,
        *,
        feature: str,
        user_id: str = "",
    ) -> None:
        if self.image_index is None or not source_key:
            return
        illust_id = str(illust.get("id") or "")
        if not illust_id:
            return
        try:
            await self.image_index.record_usage(
                scope=self._event_scope(event),
                source_key=source_key,
                illust_id=illust_id,
                feature=feature,
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 写入当天发图索引失败: {e}")

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
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 签到卡片渲染失败，回退纯文字: {e}")

        if card_path:
            try:
                content = [Image.fromFileSystem(str(card_path))]
                if background and background.pixiv_caption:
                    content.append(Plain(background.pixiv_caption))
                await event.send(event.chain_result(content))
                await self._record_checkin_background(event, background)
                claim_held = False
                if (
                    background
                    and background.image_path
                    and background.mode == "pixiv_daily"
                ):
                    cleanup(background.image_path)
                return
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 签到卡片发送失败，回退纯文字: {e}")
        if claim_held:
            await self._release_checkin_background_claim(event, background)
        if background and background.image_path and background.mode == "pixiv_daily":
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

        content = resolve_checkin_content(
            record,
            self._checkin_profile_from_record(record),
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

        greeting, source = await self.checkin_greeting.generate(
            event,
            content.context,
            enabled=self._cfg_bool("checkin_ai_greeting_enabled", False),
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
        if source != "ai":
            return record
        return await self.checkin_store.update_record_content(
            user_id=record.user_id,
            date_key=record.date_key,
            event_key=content.event_key,
            event_label=content.event_label,
            greeting=greeting,
            greeting_source="ai",
            secondary_note=content.secondary_note,
            template_version=record.template_version or "v2",
        )

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
        )
        view_model["background_mode"] = identity_background.mode
        view_model["background_source"] = identity_background.source
        return self.checkin_cache.cache_key(
            date_key=record.date_key,
            user_id=record.user_id,
            template_version=record.template_version or "v2",
            view_model=view_model,
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

    async def _handle_checkin_preview(self, event: AstrMessageEvent):
        user_id = str(event.get_sender_id() or "debug")
        username = self._event_username(event, user_id)
        bot_name = self._cfg_str("checkin_bot_name", "neko") or "neko"
        date_key = CheckinStore.today_key()
        now = CheckinStore.now_iso()
        profile = CheckinProfile(
            user_id=user_id,
            coins=888,
            affection=66.6,
            total_days=12,
            streak_days=5,
            last_checkin_date=date_key,
            boost_start_date="",
            boost_until_date="",
            repeat_penalty_date="",
            repeat_penalty_total=0.0,
            created_at=now,
            updated_at=now,
        )
        record = CheckinRecord(
            date_key=date_key,
            user_id=user_id,
            username=username,
            bot_name=bot_name,
            base_coins=88,
            bonus_coins=12,
            coins_reward=100,
            base_affection=0.88,
            bonus_affection=0.12,
            affection_reward=1.0,
            boost_active=False,
            boost_multiplier=1.0,
            total_coins_after=profile.coins,
            total_affection_after=profile.affection,
            total_days_after=profile.total_days,
            streak_days_after=profile.streak_days,
            note="签到测试预览，不会写入签到数据。",
            background_mode="",
            background_source="",
            background_illust_id="",
            background_title="",
            background_author="",
            created_at=now,
            updated_at=now,
        )
        result = CheckinResult(profile=profile, record=record, duplicate=False)

        background: CardBackground | None = None
        card_path = ""
        try:
            background = await self._prepare_checkin_background(
                event, record, claim_usage=False
            )
            card_path = await self._render_checkin_card(
                event,
                profile=profile,
                record=record,
                background=background,
                bot_name=bot_name,
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 签到测试卡片渲染失败，回退纯文字: {e}")

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
                logger.warning(f"{LOG_PREFIX} 签到测试卡片发送失败，回退纯文字: {e}")
            finally:
                cleanup(card_path)
                if background and background.image_path and background.mode == "pixiv_daily":
                    cleanup(background.image_path)

        if background and background.image_path and background.mode == "pixiv_daily":
            cleanup(background.image_path)
        yield event.plain_result(
            "签到测试预览（未写入数据）\n" + self._format_checkin_plain_text(result)
        )

    async def _handle_checkin_export(self, event: AstrMessageEvent):
        if self.checkin_store is None:
            return event.plain_result("签到数据尚未初始化，请稍后再试")
        try:
            export_path = await self._write_checkin_snapshot_backup(
                prefix="checkin-export"
            )
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 导出签到备份失败: {e}")
            return event.plain_result(f"导出签到备份失败: {e}")
        try:
            await event.send(
                event.chain_result([File(name=export_path.name, file=str(export_path))])
            )
            return None
        except Exception as e:
            logger.error(f"{LOG_PREFIX} 发送签到备份文件失败: {e}")
            return event.plain_result(f"发送签到备份文件失败: {e}")

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
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取签到状态失败: {e}")
            yield event.plain_result(f"读取签到状态失败: {e}")
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
            f"好感度: {profile.affection:.2f}（{level['name']}）",
            f"好感度加持: {boost_status_text(profile, today)}",
        ]
        yield event.plain_result("\n".join(lines))

    async def _handle_buy_checkin_boost(self, event: AstrMessageEvent, days: str):
        if not self._cfg_bool("checkin_enabled", True):
            yield event.plain_result("签到功能已关闭")
            return
        if self.checkin_store is None:
            yield event.plain_result("签到数据尚未初始化，请稍后再试")
            return
        if not days or not days.isdigit():
            yield event.plain_result("用法: /购买加持 <1|3|7>\n示例: /购买加持 3")
            return
        user_id = str(event.get_sender_id() or "")
        if not user_id:
            yield event.plain_result("无法识别用户 ID，暂时不能购买加持")
            return
        try:
            purchase = await self.checkin_store.purchase_boost(
                user_id=user_id,
                days=int(days),
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 购买签到加持失败: {e}")
            yield event.plain_result(f"购买失败: {e}")
            return
        lines = [purchase.message, f"当前金币: {purchase.profile.coins}"]
        if purchase.success:
            lines.append(
                f"好感度加持: {boost_status_text(purchase.profile, CheckinStore.today_key())}"
            )
        yield event.plain_result("\n".join(lines))

    @staticmethod
    def _build_checkin_shop() -> str:
        lines = [
            "签到商店",
            "金币可购买好感度双倍加持，加持只影响之后签到获得的好感度。",
        ]
        for days, cost in BOOST_PRODUCTS.items():
            lines.append(f"/购买加持 {days} - {days} 天，{cost} 金币")
        return "\n".join(lines)

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

    async def _render_checkin_card(
        self,
        event: AstrMessageEvent,
        *,
        profile,
        record,
        background: CardBackground | None,
        bot_name: str,
    ) -> str:
        avatar_url = self._checkin_avatar_url(event) if self._cfg_bool("checkin_avatar_enabled", True) else ""
        width = CHECKIN_CARD_WIDTH
        height = CHECKIN_CARD_HEIGHT
        data = build_checkin_card_data(
            profile=profile,
            record=record,
            bot_name=bot_name,
            avatar_url=avatar_url,
            background=background,
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
                "quality": 88,
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

    @staticmethod
    def _split_config_tags(value: str) -> list[str]:
        return [tag.strip() for tag in value.split(",") if tag.strip()]

    def _blacklist_tags(self) -> set[str]:
        return {
            tag.casefold()
            for tag in self._split_config_tags(self._cfg_str("blacklist_tags", ""))
        } or {
            tag.casefold() for tag in self._split_config_tags(DEFAULT_BLACKLIST_TAGS)
        }

    @staticmethod
    def _illust_tag_names(illust: dict) -> set[str]:
        names: set[str] = set()
        for tag in illust.get("tags") or []:
            if not isinstance(tag, dict):
                continue
            for key in ("name", "translated_name"):
                value = str(tag.get(key) or "").strip()
                if value:
                    names.add(value.casefold())
        return names

    def _matched_blacklist_tag(self, illust: dict) -> str:
        blacklist_tags = self._blacklist_tags()
        if not blacklist_tags:
            return ""
        matched = self._illust_tag_names(illust) & blacklist_tags
        return sorted(matched)[0] if matched else ""

    async def _blacklist_reason_for_illust(
        self, illust: dict, illust_id: str = ""
    ) -> str:
        illust_id = str(illust_id or illust.get("id") or "")
        matched_tag = self._matched_blacklist_tag(illust)
        if matched_tag:
            return f"作品 {illust_id or '-'} 命中拉黑标签 {matched_tag}"
        if await self._is_blacklisted_illust(illust_id):
            return f"作品 {illust_id} 已在黑名单中"
        return ""

    @staticmethod
    def _event_username(event: AstrMessageEvent, default: str) -> str:
        """从事件中提取用户昵称。"""
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

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
        *,
        ranking_override: str = "",
    ):
        """搜索并发送图片。ranking_override 非空时覆盖配置中的排行榜类型。"""
        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
            logger.warning(
                f"{LOG_PREFIX} 用户 {event.get_sender_id()} 触发频率限制，需等待 {wait} 秒"
            )
            yield event.plain_result(f"⏳ 请求太频繁，请 {wait} 秒后再试")
            return

        # 参数解析
        max_count = self._cfg_int("max_count", 5, 1, 20)
        try:
            count = max(1, min(int(count_str), max_count)) if count_str else 1
        except (TypeError, ValueError):
            count = 1

        r18_mode = self._cfg_int("pixiv_r18", 0, 0, 2)
        ranking_mode = ranking_override or self._cfg_str("pixiv_ranking_mode", "week")
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        quality = self._cfg_str("image_quality", "original")
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            100.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)
        ai_enabled = self._cfg_bool("ai_enabled", False)
        ai_prob = self._cfg_int("ai_probability", 30, 0, 100)
        ai_max = self._cfg_int("ai_max_images", 3, 1, 20)
        ai_pre_msg = self._cfg_str("ai_pre_message", "让我先品鉴一番，你稍等喵~")
        ai_vision_pid = self._cfg_str("ai_vision_provider_id", "")
        ai_comment_pid = self._cfg_str("ai_comment_provider_id", "")
        ai_vision_prompt = self._cfg_str(
            "ai_vision_prompt",
            "请详细描述这张插画的内容，包括画风、构图、配色、角色特征、表情、姿势、背景等。用简洁的中文描述。",
        )
        ai_comment_prompt = self._cfg_str(
            "ai_comment_prompt",
            "你是一个 Pixiv 插画鉴赏专家。根据以下图片描述，用轻松有趣的语气写一句简短评论（50字以内）。\n\n图片描述：{description}",
        )
        filter_manga = self._cfg_bool("filter_manga", True)

        if ranking_mode not in RANKING_MODES:
            ranking_mode = "week"

        # 获取作品列表（带分页游标）
        illusts, raw_count, source_key = await self._fetch_paginated(
            event, tag, ranking_mode
        )
        logger.info(
            f"{LOG_PREFIX} 搜索: tag={tag!r} rank={ranking_mode} count={count} "
            f"quality={quality} raw_count={raw_count}"
        )

        if not illusts:
            yield event.plain_result("❌ Pixiv 请求失败或无结果，换个标签试试")
            return

        # R18 过滤
        illusts = self._filter_r18(illusts, r18_mode)
        if not illusts:
            if r18_mode == 0:
                yield event.plain_result(
                    "🔒 过滤后没有可用作品。如果目标内容包含敏感作品，请到 Pixiv 官网「设置 > 显示设置」中开启「显示作品」选项，然后将插件 R18 设置改为 2（混合）。"
                )
            elif r18_mode == 1:
                yield event.plain_result(
                    "🔒 没有找到 R18 作品。请确认你的 Pixiv 账号已在官网「显示设置」中开启了「显示敏感作品」和「显示 R-18 作品」。"
                )
            else:
                yield event.plain_result("🔒 过滤后没有可用作品")
            return

        # 漫画过滤：普通搜索/排行中只要作品类型命中 manga 就过滤；漫画日榜保留后门。
        is_manga_ranking = not tag and ranking_mode == "day_manga"
        if filter_manga and not is_manga_ranking:
            illusts = self._filter_manga(illusts)
            if not illusts:
                yield event.plain_result(
                    "😶 过滤漫画后没有可用作品，可关闭漫画过滤后重试"
                )
                return

        illusts = await self._filter_blacklisted_illusts(illusts)
        if not illusts:
            yield event.plain_result(
                "😶 可用作品都被黑名单过滤了，换个标签或调整黑名单后再试"
            )
            return

        pick_count = min(count, len(illusts))
        dedupe_ttl_hours = self._cfg_float("dedupe_ttl_hours", 24.0, 0.0, 720.0)

        chosen = await self._pick_illusts(
            event,
            illusts,
            pick_count,
            tag=tag,
            ranking_mode=ranking_mode,
            dedupe_enabled=dedupe_ttl_hours > 0,
            raw_count=raw_count,
        )
        if not chosen:
            yield event.plain_result(
                "今天这个范围内没有未发送过的图片了，换个标签或明天再试"
            )
            return
        pick_count = len(chosen)
        pending_illust_ids: set[str] = set()
        sent_illust_ids: set[str] = set()
        if dedupe_ttl_hours > 0 and self.image_index is not None:
            pending_illust_ids = {
                str(illust.get("id") or "") for illust in chosen if illust.get("id")
            }

        # 判断发送模式
        send_as_forward = self._cfg_bool("send_as_forward", True)
        history_source = f"search:{tag.strip()}" if tag else f"rank:{ranking_mode}"

        # 下载所有图片
        downloaded: list[tuple[dict, str, str, int]] = []
        temp_paths: list[str] = []
        try:
            for idx, illust in enumerate(chosen, 1):
                illust_id = illust.get("id", "?")
                title = illust.get("title", "无标题")

                try:
                    path, actual_q, file_size = await self.downloader.download_for_send(
                        illust,
                        quality,
                        proxy=self._cfg_str("pixiv_proxy_url"),
                        timeout=timeout_sec,
                        downgrade_limit_bytes=downgrade_limit_bytes,
                        log_context=f"[{idx}/{pick_count}] 作品 {illust_id} 「{title}」",
                    )
                    logger.info(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 下载完成 {illust_id} -> {path} ({file_size / 1024:.1f} KB, quality={actual_q})"
                    )
                    temp_paths.append(path)
                    downloaded.append((illust, path, actual_q, file_size))
                except asyncio.TimeoutError:
                    logger.warning(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} 下载超时 ({timeout_sec}s)"
                    )
                except Exception as e:
                    logger.error(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} 下载失败: {e}"
                    )

            # AI 识图（每张图片单独评论）
            ai_comments: dict[int, str] = {}  # illust_id -> comment
            if ai_enabled and ai_prob > 0 and downloaded:
                if random.randint(1, 100) <= ai_prob:
                    logger.info(f"{LOG_PREFIX} 触发 AI 识图 (概率 {ai_prob}%)")
                    if ai_pre_msg:
                        await event.send(event.plain_result(ai_pre_msg))
                    # 并发分析图片（受 ai_max 限制）
                    to_analyze = downloaded[:ai_max]
                    if len(downloaded) > ai_max:
                        logger.info(
                            f"{LOG_PREFIX} [AI] 共 {len(downloaded)} 张图，仅分析前 {ai_max} 张"
                        )

                    async def _analyze(idx: int, illust: dict, path: str):
                        try:
                            comment = await self.ai.comment(
                                event,
                                path,
                                ai_vision_pid,
                                ai_comment_pid,
                                ai_vision_prompt,
                                ai_comment_prompt,
                            )
                            if comment:
                                ai_comments[illust.get("id", 0)] = comment
                                logger.info(
                                    f"{LOG_PREFIX} [AI] 作品 {illust.get('id')} 评论完成: {comment[:40]}..."
                                )
                        except Exception as e:
                            illust_id = illust.get("id", 0)
                            logger.warning(
                                f"{LOG_PREFIX} [AI] 作品 {illust_id} 识图失败: {e}，已降级跳过"
                            )
                            ai_comments[illust_id] = "羞死啦 羞死啦 ~"

                    await asyncio.gather(
                        *[
                            _analyze(i, il, p)
                            for i, (il, p, _actual_q, _file_size) in enumerate(
                                to_analyze
                            )
                        ]
                    )

            # 统一发送（避免 yield 和 send 混用导致消息拆分）
            if not downloaded:
                yield event.plain_result("😢 所有图片均下载失败，请稍后再试")
                return

            # 非 OneBot 平台不支持合并转发，自动降级
            is_onebot = event.get_platform_name() == "aiocqhttp"
            use_forward = send_as_forward and is_onebot

            if use_forward:
                # 合并转发模式：所有图片打包成一条聊天记录
                try:
                    self_id = int(event.get_self_id())
                except (TypeError, ValueError):
                    self_id = 0
                nodes = Nodes([])
                for illust, path, _actual_q, _file_size in downloaded:
                    title = illust.get("title", "无标题")
                    illust_id = illust.get("id", "?")
                    content = [
                        Plain(f"🎨 {title} (ID: {illust_id})"),
                        Image.fromFileSystem(path),
                    ]
                    # AI 评论和图片放在同一个 Node 里
                    comment = ai_comments.get(illust_id, "")
                    if comment:
                        content.append(Plain(f"🐱： {comment}"))
                    nodes.nodes.append(
                        Node(
                            uin=self_id,
                            name="Pixiv",
                            content=content,
                        )
                    )
                # 如果有下载失败的图片，在合并消息末尾提示
                failed_count = pick_count - len(downloaded)
                if failed_count > 0:
                    failed_ids = [
                        str(il.get("id", "?"))
                        for il in chosen
                        if not any(d[0].get("id") == il.get("id") for d in downloaded)
                    ]
                    nodes.nodes.append(
                        Node(
                            uin=self_id,
                            name="Pixiv",
                            content=[
                                Plain(
                                    f"⚠️ {failed_count} 张图片下载失败（ID: {', '.join(failed_ids)}），已跳过"
                                )
                            ],
                        )
                    )
                # 合并转发（带重试机制）
                max_retries = 3
                forward_success = False
                for attempt in range(1, max_retries + 1):
                    try:
                        await event.send(event.chain_result([nodes]))
                        logger.info(
                            f"{LOG_PREFIX} 合并转发 {len(nodes.nodes)} 条作品"
                            + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                        )
                        forward_success = True
                        break
                    except (asyncio.TimeoutError, Exception) as e:
                        if attempt < max_retries:
                            wait_sec = attempt * 2
                            logger.warning(
                                f"{LOG_PREFIX} 合并转发失败 (第{attempt}次): {e}，{wait_sec}秒后重试..."
                            )
                            await asyncio.sleep(wait_sec)
                        else:
                            friendly_err = self._friendly_send_error(e)
                            logger.warning(
                                f"{LOG_PREFIX} 合并转发失败 (已重试{max_retries}次): {friendly_err} | 原始错误: {e}，降级为逐条发送"
                            )

                # 合并转发失败，降级为逐条发送
                if not forward_success:
                    await event.send(
                        event.plain_result("⚠️ 合并转发失败，正在逐条发送...")
                    )
                    for illust, path, actual_q, file_size in downloaded:
                        title = illust.get("title", "无标题")
                        illust_id = illust.get("id", "?")
                        content = [
                            Plain(f"🎨 {title} (ID: {illust_id})"),
                            Image.fromFileSystem(path),
                        ]
                        comment = ai_comments.get(illust_id, "")
                        if comment:
                            content.append(Plain(f"🐱： {comment}"))
                        # 逐条发送（带重试机制）
                        for attempt in range(1, max_retries + 1):
                            try:
                                await event.send(event.chain_result(content))
                                logger.info(
                                    f"{LOG_PREFIX} [降级] 作品 {illust_id} 已发送"
                                )
                                await self._record_sent_image(
                                    event,
                                    illust,
                                    path,
                                    source=history_source,
                                    quality=actual_q,
                                    file_size=file_size,
                                )
                                await self._record_image_usage(
                                    event,
                                    source_key,
                                    illust,
                                    feature="normal",
                                    user_id=str(event.get_sender_id() or ""),
                                )
                                sent_illust_ids.add(str(illust.get("id") or ""))
                                break
                            except (asyncio.TimeoutError, Exception) as e:
                                if attempt < max_retries:
                                    await asyncio.sleep(attempt * 2)
                                else:
                                    friendly_err = self._friendly_send_error(e)
                                    logger.error(
                                        f"{LOG_PREFIX} [降级] 作品 {illust_id} 发送失败: {friendly_err} | 原始错误: {e}"
                                    )
                                    try:
                                        await event.send(
                                            event.plain_result(
                                                f"⚠️ 作品 {illust_id}「{title}」发送失败，已跳过"
                                            )
                                        )
                                    except Exception:
                                        pass
                else:
                    for illust, path, actual_q, file_size in downloaded:
                        await self._record_sent_image(
                            event,
                            illust,
                            path,
                            source=history_source,
                            quality=actual_q,
                            file_size=file_size,
                        )
                        await self._record_image_usage(
                            event,
                            source_key,
                            illust,
                            feature="normal",
                            user_id=str(event.get_sender_id() or ""),
                        )
                        sent_illust_ids.add(str(illust.get("id") or ""))
            else:
                # 逐条发送模式
                for illust, path, actual_q, file_size in downloaded:
                    title = illust.get("title", "无标题")
                    illust_id = illust.get("id", "?")
                    content = [
                        Plain(f"🎨 {title} (ID: {illust_id})"),
                        Image.fromFileSystem(path),
                    ]
                    comment = ai_comments.get(illust_id, "")
                    if comment:
                        content.append(Plain(f"🐱： {comment}"))
                    # 逐条发送（带重试机制）
                    max_retries = 3
                    for attempt in range(1, max_retries + 1):
                        try:
                            await event.send(event.chain_result(content))
                            logger.info(
                                f"{LOG_PREFIX} 作品 {illust_id} 已发送"
                                + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                            )
                            await self._record_sent_image(
                                event,
                                illust,
                                path,
                                source=history_source,
                                quality=actual_q,
                                file_size=file_size,
                            )
                            await self._record_image_usage(
                                event,
                                source_key,
                                illust,
                                feature="normal",
                                user_id=str(event.get_sender_id() or ""),
                            )
                            sent_illust_ids.add(str(illust.get("id") or ""))
                            break
                        except (asyncio.TimeoutError, Exception) as e:
                            if attempt < max_retries:
                                wait_sec = attempt * 2
                                logger.warning(
                                    f"{LOG_PREFIX} 作品 {illust_id} 发送失败 (第{attempt}次): {e}，{wait_sec}秒后重试..."
                                )
                                await asyncio.sleep(wait_sec)
                            else:
                                friendly_err = self._friendly_send_error(e)
                                logger.error(
                                    f"{LOG_PREFIX} 作品 {illust_id} 发送失败 (已重试{max_retries}次): {friendly_err} | 原始错误: {e}"
                                )
                                try:
                                    await event.send(
                                        event.plain_result(
                                            f"⚠️ 作品 {illust_id}「{title}」发送失败，已跳过"
                                        )
                                    )
                                except Exception:
                                    pass
        finally:
            for p in temp_paths:
                cleanup(p)
            if pending_illust_ids and self.image_index is not None:
                for illust_id in pending_illust_ids - sent_illust_ids:
                    try:
                        await self.image_index.release_usage(
                            scope=self._event_scope(event),
                            source_key=source_key,
                            illust_id=illust_id,
                            feature="normal_pending",
                        )
                    except Exception as e:
                        logger.warning(f"{LOG_PREFIX} 释放当天发图占用失败: {e}")

    async def _handle_rank(
        self, event: AstrMessageEvent, mode: str, count_str: str = ""
    ):
        """排行榜模式。"""
        mode = mode.lower().strip() if mode else "week"

        if mode not in RANKING_MODES:
            yield event.plain_result(
                f"⚠️ 未知排行榜类型: {mode}\n发送 /prl 查看所有类型"
            )
            return

        # 走搜索逻辑，通过参数传递排行榜类型
        async for result in self._handle_search(
            event, tag="", count_str=count_str, ranking_override=mode
        ):
            yield result

    async def _handle_info(self, event: AstrMessageEvent, illust_id: int):
        """查看作品详情。"""

        try:
            illust = await self.client.illust_detail(illust_id)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 获取作品详情失败 illust_id={illust_id}: {e}")
            yield event.plain_result("❌ 获取作品详情失败，请稍后再试")
            return

        if not illust:
            yield event.plain_result(f"😶 未找到作品 {illust_id}")
            return

        reason = await self._blacklist_reason_for_illust(illust, str(illust_id))
        if reason:
            yield event.plain_result(f"🚫 {reason}")
            return

        title = illust.get("title", "无标题")
        author = (illust.get("user") or {}).get("name", "未知")
        tags = "、".join(t.get("name", "") for t in (illust.get("tags") or [])[:5])
        desc = (illust.get("caption") or "").strip()
        pages = len(illust.get("meta_pages") or [])
        x_restrict = illust.get("x_restrict", 0)
        total_view = illust.get("total_view", 0)
        total_bookmark = illust.get("total_bookmark", 0)

        lines = [
            "🎨 作品详情",
            f"ID: {illust_id}",
            f"标题: {title}",
            f"作者: {author}",
            f"标签: {tags or '无'}",
            f"页数: {pages or 1}",
            f"R18: {'是' if x_restrict else '否'}",
            f"浏览: {total_view:,}　收藏: {total_bookmark:,}",
        ]
        if desc:
            desc = re.sub(r"<[^>]+>", "", desc).strip()
            if desc:
                lines.append(f"简介: {desc[:200]}")
        lines.append(f"链接: https://www.pixiv.net/artworks/{illust_id}")

        yield event.plain_result("\n".join(lines))

    async def _handle_download(
        self, event: AstrMessageEvent, illust_id: int, page: int = 1
    ):
        """通过作品ID下载并发送图片。"""

        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
            logger.warning(
                f"{LOG_PREFIX} 用户 {event.get_sender_id()} 触发频率限制，需等待 {wait} 秒"
            )
            yield event.plain_result(f"⏳ 请求太频繁，请 {wait} 秒后再试")
            return

        # 获取作品详情
        try:
            illust = await self.client.illust_detail(illust_id)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 下载前获取作品详情失败 illust_id={illust_id}: {e}")
            yield event.plain_result("❌ 获取作品详情失败，请稍后再试")
            return

        if not illust:
            yield event.plain_result(f"😶 未找到作品 {illust_id}")
            return

        reason = await self._blacklist_reason_for_illust(illust, str(illust_id))
        if reason:
            yield event.plain_result(f"🚫 {reason}")
            return

        # R18 检查
        r18_mode = self._cfg_int("pixiv_r18", 0, 0, 2)
        x_restrict = int(illust.get("x_restrict", 0) or 0)
        if r18_mode == 0 and x_restrict > 0:
            yield event.plain_result("🔒 该作品为 R18 内容，当前配置不允许下载")
            return
        if r18_mode == 1 and x_restrict == 0:
            yield event.plain_result(
                "🔒 该作品非 R18 内容，当前配置仅允许下载 R18 作品"
            )
            return

        title = illust.get("title", "无标题")
        meta_pages = illust.get("meta_pages") or []
        total_pages = len(meta_pages) if meta_pages else 1

        # 页码校验
        if page < 1 or page > total_pages:
            if total_pages == 1:
                yield event.plain_result(
                    f"⚠️ 该作品只有 1 页，不需要指定页码\n用法: /pd {illust_id}"
                )
            else:
                yield event.plain_result(
                    f"⚠️ 页码无效，该作品共 {total_pages} 页\n用法: /pd {illust_id} [1-{total_pages}]"
                )
            return

        # 获取指定页码的图片URL
        quality = self._cfg_str("image_quality", "original")
        proxy = self._cfg_str("pixiv_proxy_url")
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            100.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)

        if total_pages > 1:
            # 多页作品：获取指定页的URL
            page_data = meta_pages[page - 1] if page - 1 < len(meta_pages) else {}
            page_urls = page_data.get("image_urls") or {}
            quality_order = {
                "original": ["original", "large", "medium", "square_medium"],
                "large": ["large", "medium", "square_medium", "original"],
                "medium": ["medium", "square_medium", "large", "original"],
            }
            order = quality_order.get(quality, quality_order["original"])
            url = ""
            for q in order:
                if page_urls.get(q):
                    url = page_urls[q]
                    break
            if not url:
                yield event.plain_result(f"😢 作品 {illust_id} 第 {page} 页无可下载URL")
                return
        else:
            # 单页作品
            url = pick_image_url(illust, quality)
            if not url:
                yield event.plain_result(f"😢 作品 {illust_id} 无可下载URL")
                return

        # 下载图片
        log_context = f"作品 {illust_id}「{title}」第 {page}/{total_pages} 页"
        logger.info(f"{LOG_PREFIX} 下载 {log_context} quality={quality}")

        try:
            path = await self.downloader.download(url, proxy=proxy, timeout=timeout_sec)
            file_size = os.path.getsize(path)

            # 原图自动降级
            actual_quality = (
                "original"
                if "original" in url
                else "large"
                if "large" in url
                else "medium"
                if "medium" in url
                else "square_medium"
            )
            if (
                downgrade_limit_bytes > 0
                and actual_quality == "original"
                and file_size > downgrade_limit_bytes
            ):
                logger.info(f"{LOG_PREFIX} {log_context} 原图超过阈值，尝试降级")
                cleanup(path)

                # 尝试降级
                for candidate_quality in ("large", "medium", "square_medium"):
                    if total_pages > 1:
                        candidate_url = page_urls.get(candidate_quality, "")
                    else:
                        candidate_url = pick_image_url_exact(illust, candidate_quality)
                    if not candidate_url or candidate_url == url:
                        continue
                    try:
                        path = await self.downloader.download(
                            candidate_url, proxy=proxy, timeout=timeout_sec
                        )
                        file_size = os.path.getsize(path)
                        actual_quality = candidate_quality
                        logger.info(
                            f"{LOG_PREFIX} {log_context} 降级到 {candidate_quality} ({file_size / 1024:.1f} KB)"
                        )
                        break
                    except Exception as e:
                        logger.warning(
                            f"{LOG_PREFIX} {log_context} 降级到 {candidate_quality} 失败: {e}"
                        )
                        continue
                else:
                    yield event.plain_result("😢 原图过大且降级图片不可用")
                    return

            logger.info(
                f"{LOG_PREFIX} 下载完成 {log_context} -> {path} ({file_size / 1024:.1f} KB)"
            )

            # 构建发送内容
            content = [
                Plain(f"🎨 {title} (ID: {illust_id}, 第 {page}/{total_pages} 页)"),
                Image.fromFileSystem(path),
            ]

            # AI 识图
            ai_enabled = self._cfg_bool("ai_enabled", False)
            ai_prob = self._cfg_int("ai_probability", 30, 0, 100)
            if ai_enabled and ai_prob > 0 and random.randint(1, 100) <= ai_prob:
                ai_pre_msg = self._cfg_str(
                    "ai_pre_message", "让我先品鉴一番，你稍等喵~"
                )
                if ai_pre_msg:
                    await event.send(event.plain_result(ai_pre_msg))
                try:
                    ai_vision_pid = self._cfg_str("ai_vision_provider_id", "")
                    ai_comment_pid = self._cfg_str("ai_comment_provider_id", "")
                    ai_vision_prompt = self._cfg_str(
                        "ai_vision_prompt",
                        "请详细描述这张插画的内容，包括画风、构图、配色、角色特征、表情、姿势、背景等。用简洁的中文描述。",
                    )
                    ai_comment_prompt = self._cfg_str(
                        "ai_comment_prompt",
                        "你是一个 Pixiv 插画鉴赏专家。根据以下图片描述，用轻松有趣的语气写一句简短评论（50字以内）。\n\n图片描述：{description}",
                    )
                    comment = await self.ai.comment(
                        event,
                        path,
                        ai_vision_pid,
                        ai_comment_pid,
                        ai_vision_prompt,
                        ai_comment_prompt,
                    )
                    if comment:
                        content.append(Plain(f"🐱： {comment}"))
                except Exception as e:
                    logger.warning(f"{LOG_PREFIX} [AI] 识图失败: {e}")

            # 发送（带重试机制，最多3次）
            max_retries = 3
            send_success = False
            for attempt in range(1, max_retries + 1):
                try:
                    await event.send(event.chain_result(content))
                    logger.info(
                        f"{LOG_PREFIX} 已发送 {log_context}"
                        + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                    )
                    send_success = True
                    break
                except (asyncio.TimeoutError, Exception) as e:
                    if attempt < max_retries:
                        wait_sec = attempt * 2  # 递增等待：2秒、4秒
                        logger.warning(
                            f"{LOG_PREFIX} {log_context} 发送失败 (第{attempt}次): {e}，{wait_sec}秒后重试..."
                        )
                        await asyncio.sleep(wait_sec)
                    else:
                        friendly_err = self._friendly_send_error(e)
                        logger.error(
                            f"{LOG_PREFIX} {log_context} 发送失败 (已重试{max_retries}次): {friendly_err} | 原始错误: {e}"
                        )
                        yield event.plain_result(
                            f"😢 发送失败（已重试{max_retries}次），请稍后再试"
                        )

            if send_success:
                await self._record_sent_image(
                    event,
                    illust,
                    path,
                    source="download",
                    page=page,
                    quality=actual_quality,
                    file_size=file_size,
                )

        except asyncio.TimeoutError:
            logger.warning(f"{LOG_PREFIX} {log_context} 下载超时 ({timeout_sec}s)")
            yield event.plain_result("😢 下载超时，请稍后再试")
        except Exception as e:
            logger.error(f"{LOG_PREFIX} {log_context} 下载失败: {e}")
            yield event.plain_result(f"😢 下载失败: {e}")
        finally:
            # 清理临时文件
            if "path" in locals() and path:
                cleanup(path)

    # ──────────────────────────────────────────────────────────────
    # 帮助
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
            "签到测试",
            "　　管理员预览签到卡片，不写入签到数据",
            "",
            "签到导出",
            "　　管理员导出签到完整备份文件",
            "",
            "签到状态",
            "　　查看累计签到、金币、好感度和加持状态",
            "",
            "签到商店 / 购买加持",
            "　　查看并购买好感度双倍加持",
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

    @staticmethod
    def _filter_r18(illusts: list[dict], mode: int) -> list[dict]:
        """根据 R18 模式过滤作品。mode: 0=仅非R18, 1=仅R18, 2=混合。"""

        def keep(illust: dict) -> bool:
            xr = int(illust.get("x_restrict", 0) or 0)
            if mode == 0:
                return xr == 0
            if mode == 1:
                return xr > 0
            return True

        return [i for i in illusts if keep(i)]

    @staticmethod
    def _filter_manga(illusts: list[dict]) -> list[dict]:
        """Filter out every Pixiv manga item."""
        return [il for il in illusts if il.get("type") != "manga"]

    async def _filter_blacklisted_illusts(self, illusts: list[dict]) -> list[dict]:
        if not illusts:
            return illusts
        blacklist_tags = self._blacklist_tags()
        blacklisted: set[str] = set()
        if self.image_index is None and not blacklist_tags:
            return illusts
        try:
            if self.image_index is not None:
                blacklisted = await self.image_index.get_blacklisted_illust_ids()
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取图片黑名单失败: {e}")
        if not blacklisted and not blacklist_tags:
            return illusts
        return [
            illust
            for illust in illusts
            if str(illust.get("id") or "") not in blacklisted
            and not self._matched_blacklist_tag(illust)
        ]

    async def _is_blacklisted_illust(self, illust_id: str) -> bool:
        if self.image_index is None or not illust_id:
            return False
        try:
            return await self.image_index.is_blacklisted(illust_id)
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取图片黑名单失败: {e}")
            return False

    @staticmethod
    def _friendly_send_error(error: Exception) -> str:
        """生成友善的发送错误提示。"""
        error_str = str(error).lower()
        if isinstance(error, asyncio.TimeoutError) or "timeout" in error_str:
            return "图片上传超时，可能是图片太大或网络较慢，建议降低图片质量设置"
        if "cdn" in error_str or "upload" in error_str:
            return "图片上传到服务器失败，请稍后再试"
        if "network" in error_str or "connect" in error_str:
            return "网络连接异常，请检查网络后重试"
        return "发送失败，请稍后再试"

    async def _pick_illusts(
        self,
        event: AstrMessageEvent,
        illusts: list[dict],
        pick_count: int,
        *,
        tag: str,
        ranking_mode: str,
        dedupe_enabled: bool = True,
        raw_count: int = 0,
    ) -> list[dict]:
        if not dedupe_enabled or self.image_index is None:
            return random.sample(illusts, pick_count)

        source_key = self._source_key(tag, ranking_mode)
        scope = self._event_scope(event)
        try:
            used_ids = await self.image_index.get_used_illust_ids(
                scope, source_key
            )
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 读取当天发图索引失败: {e}")
            return []

        ordered = ordered_by_unused(illusts, used_ids)
        fresh = [i for i in ordered if str(i.get("id") or "") not in used_ids]
        repeated = [i for i in ordered if str(i.get("id") or "") in used_ids]

        # 若整页全被用过，推进分页游标供下次翻页
        if not fresh and raw_count > 0:
            try:
                await self.image_index.advance_page_offset(
                    scope, source_key, raw_count
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 分页游标更新失败: {e}")

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
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 占用当天发图索引失败: {e}")
                return chosen
            if claimed:
                chosen.append(illust)
        return chosen

    def _check_rate_limit(self, user_id: str) -> int:
        """检查用户请求频率，返回需等待秒数（0 表示可立即请求）。"""
        rate_limit = self._cfg_int("rate_limit_seconds", 3, 0, 60)
        if rate_limit <= 0:
            return 0
        now = time.monotonic()
        last = self._last_request.get(user_id, 0.0)
        elapsed = now - last
        if elapsed < rate_limit:
            return int(rate_limit - elapsed) + 1
        self._last_request[user_id] = now
        return 0

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
