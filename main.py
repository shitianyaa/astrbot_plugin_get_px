"""AstrBot 插件 — Pixiv 发图

通过标签搜索 Pixiv 插画并发送图片，支持排行榜、R18 过滤、多页作品、代理配置、自然语言自动触发和今日运势。

搜索指令：
    /p [标签] [数量]           搜索并发送图片

自动触发（需在配置中开启）：
    来一份图                   发送 1 张排行榜图片
    来三张初音ミク图             搜索标签「初音ミク」发送 3 张

管理指令：
    /pr [排行类型] [数量]      获取排行榜
    /prl                       查看所有排行榜类型
    /pi <作品ID>               查看作品详情
    /今日运势, /jrys            查看今日运势
    /ph                        查看帮助
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import random
import re
import time

from astrbot.api.all import AstrBotConfig, Image, Plain, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes
from astrbot.api.star import Context, Star

from .ai_commenter import AiCommenter
from .downloader import ImageDownloader, cleanup, pick_image_url, pick_image_url_exact
from .fortune import build_fortune, format_fortune
from .pixiv_client import PixivClient

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"

AUTO_TRIGGER_PATTERN = r"^/?(来\s*(.*?)(份|个|张|点))(.*?)(福利|色|瑟|涩|塞)?图$"
FORTUNE_REGEX_PATTERN = r"^(?!/)(今日运势|jrys)$"

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
DEFAULT_FORTUNE_AI_PROMPT = """你会收到一份已经抽好的今日运势数据，请只根据这份数据改写成适合聊天机器人发送的中文文案。

【今日运势数据】
{fortune_text}

要求：
- 必须准确保留运势等级、星级、说明、宜、忌、提示
- 不要说“用户未提供数据”，上面的【今日运势数据】就是输入数据
- 不要新增与结果冲突的信息
- 语气轻松自然，可以稍微可爱，但不要过度卖萌
- 输出纯文本，不要 Markdown，不要代码块
- 严格按示例格式输出

输出格式：
{username}
今天 {date_str} {title}
星级{stars}
{description}
宜：{good}
忌：{bad}
{extra_message}

"""


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
        self._recent_illusts: dict[str, dict[str, float]] = {}
        self._fortune_image_assignments: dict[str, str] = {}
        self._fortune_text_cache: dict[str, str] = {}

    # ──────────────────────────────────────────────────────────────
    # 生命周期
    # ──────────────────────────────────────────────────────────────

    async def initialize(self):
        """插件加载时初始化 Pixiv 客户端。"""
        self._init_client()
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

    async def terminate(self):
        """插件卸载/停用时清理资源。"""
        if self.client is not None:
            await self.client.close()
            self.client = None
        await self.downloader.close()
        self._last_request.clear()
        self._recent_illusts.clear()
        self._fortune_image_assignments.clear()
        self._fortune_text_cache.clear()
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

    @filter.command("今日运势", alias=["jrys"])
    async def cmd_fortune(self, event: AstrMessageEvent):
        """查看今日运势。"""
        event.stop_event()
        async for result in self._handle_fortune(event):
            yield result

    @filter.regex(FORTUNE_REGEX_PATTERN)
    async def fortune_auto_trigger(self, event: AstrMessageEvent):
        """纯文本触发今日运势。"""
        if not self._cfg_bool("fortune_enabled", True):
            return
        event.stop_event()
        async for result in self._handle_fortune(event, silent_when_disabled=True):
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

    async def _handle_fortune(
        self, event: AstrMessageEvent, *, silent_when_disabled: bool = False
    ):
        """生成并发送今日运势。"""
        if not self._cfg_bool("fortune_enabled", True):
            if not silent_when_disabled:
                yield event.plain_result("今日运势功能已关闭")
            return

        user_id = str(event.get_sender_id() or "")
        group_id = event.get_group_id()
        username = self._event_username(event, user_id or "用户")
        result = build_fortune(
            user_id or username, username, str(group_id) if group_id else None
        )
        fallback_text = format_fortune(result)
        fortune_text = await self._build_fortune_text(event, result, fallback_text)

        if self._cfg_bool("fortune_image_enabled", True):
            fortune_image = await self._download_fortune_image(event, result)
            if fortune_image:
                illust, image_path, dedupe_key, assignment_key = fortune_image
                try:
                    if await self._send_fortune_with_image(
                        event, fortune_text, illust, image_path
                    ):
                        self._mark_fortune_image_sent(
                            dedupe_key, assignment_key, illust
                        )
                        return
                finally:
                    cleanup(image_path)

        yield event.plain_result(fortune_text)

    async def _build_fortune_text(
        self, event: AstrMessageEvent, result, fallback_text: str
    ) -> str:
        """Build fortune text, optionally rewritten by a text model."""
        if not self._cfg_bool("fortune_ai_text_enabled", False):
            return fallback_text

        provider_id = await self.ai._resolve_provider(
            self._cfg_str("fortune_ai_provider_id", ""),
            event.unified_msg_origin,
        )
        if not provider_id:
            logger.warning(f"{LOG_PREFIX} 今日运势文案生成跳过：未配置文本模型")
            return fallback_text

        stars = "★" * result.star_count + "☆" * (result.max_stars - result.star_count)
        prompt_template = self._cfg_str("fortune_ai_prompt", "") or DEFAULT_FORTUNE_AI_PROMPT
        prompt_data = {
            "username": result.username,
            "date_str": result.date_str,
            "title": result.title,
            "star_count": result.star_count,
            "max_stars": result.max_stars,
            "stars": stars,
            "description": result.description,
            "good": result.good,
            "bad": result.bad,
            "extra_message": result.extra_message,
            "fortune_text": fallback_text,
            "fortune": fallback_text,
            "data": fallback_text,
        }
        try:
            prompt = self._format_prompt_template(prompt_template, prompt_data)
        except ValueError as e:
            logger.warning(f"{LOG_PREFIX} 今日运势文案提示词格式错误: {e}，回退预置文案")
            return fallback_text
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        cache_key = (
            f"{result.date_str}|{event.get_sender_id() or ''}|"
            f"{event.get_group_id() or 'private'}|{provider_id}|{prompt_hash}"
        )
        cached = self._fortune_text_cache.get(cache_key)
        if cached:
            return cached

        timeout_sec = self._cfg_float("fortune_ai_timeout_seconds", 15.0, 0.0, 120.0)
        try:
            generate_task = self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            if timeout_sec > 0:
                resp = await asyncio.wait_for(generate_task, timeout=timeout_sec)
            else:
                resp = await generate_task
            if isinstance(resp, str):
                text = resp.strip()
            else:
                text = (getattr(resp, "completion_text", "") or "").strip()
            if not text:
                logger.warning(f"{LOG_PREFIX} 今日运势文案模型返回空结果，回退预置文案")
                return fallback_text
            text = self._strip_code_fence(text)
            if len(text) > 600:
                text = text[:600].rstrip()
            self._fortune_text_cache[cache_key] = text
            logger.info(f"{LOG_PREFIX} 今日运势文案已由模型生成 provider={provider_id}")
            return text
        except asyncio.TimeoutError:
            logger.warning(
                f"{LOG_PREFIX} 今日运势文案生成超时 ({timeout_sec:g}s)，回退预置文案"
            )
            return fallback_text
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 今日运势文案生成失败: {e}，回退预置文案")
            return fallback_text

    @staticmethod
    def _format_prompt_template(template: str, data: dict) -> str:
        class SafeDict(dict):
            def __missing__(self, key):
                return "{" + key + "}"

        return template.format_map(SafeDict(data))

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            lines = stripped.splitlines()
            if len(lines) >= 3:
                return "\n".join(lines[1:-1]).strip()
        return stripped

    @staticmethod
    def _split_config_tags(value: str) -> list[str]:
        return [tag.strip() for tag in value.split(",") if tag.strip()]

    async def _download_fortune_image(self, event: AstrMessageEvent, result):
        """Fetch one Pixiv image for today's fortune, falling back silently on failure."""
        token = self._cfg_str("pixiv_refresh_token")
        if not token:
            logger.info(f"{LOG_PREFIX} 今日运势配图跳过：未配置 Pixiv refresh_token")
            return None
        if self.client is None:
            self._init_client()
        if self.client is None:
            return None

        self._prune_fortune_image_assignments(result.date_str)
        tag_config = self._cfg_str("fortune_image_tag", "")
        tags = self._split_config_tags(tag_config)
        selected_tag = ""
        if tags:
            tag_seed_text = (
                f"fortune-image-tag|{result.date_str}|{event.get_sender_id() or ''}|"
                f"{event.get_group_id() or 'private'}|{tag_config}"
            )
            tag_seed = int.from_bytes(
                hashlib.sha256(tag_seed_text.encode("utf-8")).digest()[:8], "big"
            )
            selected_tag = tags[tag_seed % len(tags)]
        ranking_mode = self._cfg_str("pixiv_ranking_mode", "week")
        if ranking_mode not in RANKING_MODES:
            ranking_mode = "week"

        try:
            if selected_tag:
                illusts = await self.client.search(selected_tag)
                source_desc = f"tag={selected_tag!r}"
            else:
                illusts = await self.client.ranking(ranking_mode)
                source_desc = f"rank={ranking_mode}"
        except Exception as e:
            logger.warning(f"{LOG_PREFIX} 今日运势配图获取失败: {e}")
            return None

        r18_mode = self._cfg_int("pixiv_r18", 0, 0, 2)
        illusts = self._filter_r18(illusts, r18_mode)
        is_manga_ranking = not selected_tag and ranking_mode == "day_manga"
        if self._cfg_bool("filter_manga", True) and not is_manga_ranking:
            illusts = self._filter_manga(illusts)
        if not illusts:
            logger.info(f"{LOG_PREFIX} 今日运势配图无可用作品 ({source_desc})")
            return None

        quality = self._cfg_str("image_quality", "original")
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            100.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)

        seed_text = (
            f"fortune-image|{result.date_str}|{event.get_sender_id() or ''}|"
            f"{event.get_group_id() or 'private'}|{tag_config}|{selected_tag}|{ranking_mode}"
        )
        seed = int.from_bytes(
            hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big"
        )
        start = seed % len(illusts)
        ordered = illusts[start:] + illusts[:start]
        dedupe_ttl_hours = self._cfg_float("dedupe_ttl_hours", 24.0, 0.0, 720.0)
        dedupe_key = self._fortune_image_dedupe_key(event, result.date_str)
        source_key = self._fortune_image_source_key(selected_tag, ranking_mode)
        assignment_key = self._fortune_image_assignment_key(
            event, result.date_str, source_key
        )
        ordered = self._prioritize_fortune_image_candidates(
            ordered,
            dedupe_key=dedupe_key,
            assignment_key=assignment_key,
            ttl_hours=dedupe_ttl_hours,
        )

        for idx, illust in enumerate(ordered[:5], 1):
            illust_id = illust.get("id", "?")
            title = illust.get("title", "无标题")
            try:
                path, actual_q, file_size = await self.downloader.download_for_send(
                    illust,
                    quality,
                    proxy=self._cfg_str("pixiv_proxy_url"),
                    timeout=timeout_sec,
                    downgrade_limit_bytes=downgrade_limit_bytes,
                    log_context=f"[今日运势配图 {idx}] 作品 {illust_id} 「{title}」",
                )
                logger.info(
                    f"{LOG_PREFIX} 今日运势配图下载完成 {illust_id} -> {path} "
                    f"({file_size / 1024:.1f} KB, quality={actual_q})"
                )
                return illust, path, dedupe_key, assignment_key
            except asyncio.TimeoutError:
                logger.warning(
                    f"{LOG_PREFIX} 今日运势配图 {illust_id} 下载超时 ({timeout_sec}s)"
                )
            except Exception as e:
                logger.warning(f"{LOG_PREFIX} 今日运势配图 {illust_id} 下载失败: {e}")

        return None

    async def _send_fortune_with_image(
        self, event: AstrMessageEvent, fortune_text: str, illust: dict, image_path: str
    ) -> bool:
        """Send fortune text with one Pixiv image."""
        title = illust.get("title", "无标题")
        illust_id = illust.get("id", "?")
        content = [
            Plain(fortune_text),
            Image.fromFileSystem(image_path),
            Plain(f"配图：{title} (ID: {illust_id})"),
        ]

        max_retries = 3
        for attempt in range(1, max_retries + 1):
            try:
                await event.send(event.chain_result(content))
                logger.info(
                    f"{LOG_PREFIX} 今日运势配图已发送 {illust_id}"
                    + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                )
                return True
            except (asyncio.TimeoutError, Exception) as e:
                if attempt < max_retries:
                    wait_sec = attempt * 2
                    logger.warning(
                        f"{LOG_PREFIX} 今日运势配图发送失败 (第{attempt}次): {e}，{wait_sec}秒后重试..."
                    )
                    await asyncio.sleep(wait_sec)
                    continue
                friendly_err = self._friendly_send_error(e)
                logger.warning(
                    f"{LOG_PREFIX} 今日运势配图发送失败 (已重试{max_retries}次): "
                    f"{friendly_err} | 原始错误: {e}，回退纯文字"
                )
        return False

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

        # 获取作品列表
        logger.info(
            f"{LOG_PREFIX} 搜索: tag={tag!r} rank={ranking_mode} count={count} quality={quality}"
        )
        try:
            if tag:
                illusts = await self.client.search(tag)
            else:
                illusts = await self.client.ranking(ranking_mode)
        except Exception as e:
            logger.error(f"{LOG_PREFIX} Pixiv 请求失败: {e}")
            yield event.plain_result(f"❌ Pixiv 请求失败: {e}")
            return

        if not illusts:
            yield event.plain_result("😶 没有找到作品，换个标签试试")
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

        # 漫画过滤（全是漫画时过滤掉，混合结果保留；漫画排行榜不过滤）
        if filter_manga and ranking_mode != "day_manga":
            illusts = self._filter_manga(illusts)
            if not illusts:
                yield event.plain_result(
                    "😶 过滤漫画后没有可用作品，可关闭漫画过滤后重试"
                )
                return

        pick_count = min(count, len(illusts))
        dedupe_ttl_hours = self._cfg_float("dedupe_ttl_hours", 24.0, 0.0, 720.0)
        chosen = self._pick_illusts(
            event,
            illusts,
            pick_count,
            tag=tag,
            ranking_mode=ranking_mode,
            ttl_hours=dedupe_ttl_hours,
        )

        # 判断发送模式
        send_as_forward = self._cfg_bool("send_as_forward", True)

        # 下载所有图片
        downloaded: list[tuple[dict, str]] = []  # (illust, file_path)
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
                    downloaded.append((illust, path))
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
                        *[_analyze(i, il, p) for i, (il, p) in enumerate(to_analyze)]
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
                for illust, path in downloaded:
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
                    for illust, path in downloaded:
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
                # 逐条发送模式
                for illust, path in downloaded:
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
            yield event.plain_result(f"❌ 获取作品详情失败: {e}")
            return

        if not illust:
            yield event.plain_result(f"😶 未找到作品 {illust_id}")
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
            yield event.plain_result(f"❌ 获取作品详情失败: {e}")
            return

        if not illust:
            yield event.plain_result(f"😶 未找到作品 {illust_id}")
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
            "今日运势 / jrys",
            "　　查看今日运势，也可直接发送 今日运势 或 jrys",
            "",
            "🤖 自然语言触发（需配置开启 auto_trigger_enabled）",
            "　　来一份图 → 发送 1 张排行榜图片",
            "　　来三张初音ミク图 → 搜标签发送 3 张",
            "　　来两张萝莉 → 搜标签发送 2 张",
            "",
            "⚙️ 漫画过滤（filter_manga）:",
            "　　开启后，搜索结果全是漫画时自动过滤",
            "　　混合结果（插画+漫画）保留全部",
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
        """当结果全是漫画时过滤掉；混合结果保留全部。"""
        if any(il.get("type") == "illust" for il in illusts):
            return illusts
        return [il for il in illusts if il.get("type") != "manga"]

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

    def _pick_illusts(
        self,
        event: AstrMessageEvent,
        illusts: list[dict],
        pick_count: int,
        *,
        tag: str,
        ranking_mode: str,
        ttl_hours: float,
    ) -> list[dict]:
        if ttl_hours <= 0:
            return random.sample(illusts, pick_count)

        key = self._dedupe_key(event, tag=tag, ranking_mode=ranking_mode)
        now = time.monotonic()
        recent = self._recent_illusts.setdefault(key, {})
        expires_before = now - ttl_hours * 3600
        for illust_id, sent_at in list(recent.items()):
            if sent_at < expires_before:
                recent.pop(illust_id, None)

        fresh = [i for i in illusts if str(i.get("id", "")) not in recent]
        if len(fresh) >= pick_count:
            chosen = random.sample(fresh, pick_count)
        else:
            chosen = fresh[:]
            used_ids = {str(i.get("id", "")) for i in chosen}
            fallback = [i for i in illusts if str(i.get("id", "")) not in used_ids]
            chosen.extend(random.sample(fallback, pick_count - len(chosen)))

        for illust in chosen:
            illust_id = str(illust.get("id", ""))
            if illust_id:
                recent[illust_id] = now
        return chosen

    def _prioritize_fortune_image_candidates(
        self,
        illusts: list[dict],
        *,
        dedupe_key: str,
        assignment_key: str,
        ttl_hours: float,
    ) -> list[dict]:
        if not illusts:
            return illusts

        assigned_id = self._fortune_image_assignments.get(assignment_key)
        assigned = [i for i in illusts if str(i.get("id", "")) == assigned_id]
        assigned_ids = {str(i.get("id", "")) for i in assigned}
        remaining = [i for i in illusts if str(i.get("id", "")) not in assigned_ids]

        if ttl_hours <= 0:
            return assigned + remaining

        recent = self._recent_illusts.setdefault(dedupe_key, {})
        self._prune_recent_illusts(recent, ttl_hours)
        fresh = [i for i in remaining if str(i.get("id", "")) not in recent]
        repeated = [i for i in remaining if str(i.get("id", "")) in recent]
        return assigned + fresh + repeated

    def _mark_fortune_image_sent(
        self, dedupe_key: str, assignment_key: str, illust: dict
    ) -> None:
        illust_id = str(illust.get("id", ""))
        if not illust_id:
            return

        self._fortune_image_assignments[assignment_key] = illust_id
        recent = self._recent_illusts.setdefault(dedupe_key, {})
        recent[illust_id] = time.monotonic()

    def _prune_fortune_image_assignments(self, date_str: str) -> None:
        current_prefix = f"fortune:{date_str}:"
        for key in list(self._fortune_image_assignments):
            if not key.startswith(current_prefix):
                self._fortune_image_assignments.pop(key, None)

    @staticmethod
    def _prune_recent_illusts(recent: dict[str, float], ttl_hours: float) -> None:
        expires_before = time.monotonic() - ttl_hours * 3600
        for illust_id, sent_at in list(recent.items()):
            if sent_at < expires_before:
                recent.pop(illust_id, None)

    @staticmethod
    def _fortune_image_source_key(tag: str, ranking_mode: str) -> str:
        return f"search:{tag.strip().casefold()}" if tag else f"rank:{ranking_mode}"

    @staticmethod
    def _fortune_image_dedupe_key(event: AstrMessageEvent, date_str: str) -> str:
        group_id = event.get_group_id()
        if group_id:
            scope = f"group:{group_id}"
        else:
            scope = f"private:{event.get_sender_id()}"
        return f"fortune:{date_str}:{scope}"

    @staticmethod
    def _fortune_image_assignment_key(
        event: AstrMessageEvent, date_str: str, source_key: str
    ) -> str:
        group_id = event.get_group_id()
        if group_id:
            scope = f"group:{group_id}"
        else:
            scope = f"private:{event.get_sender_id()}"
        return (
            f"fortune:{date_str}:{scope}:user:{event.get_sender_id() or ''}:"
            f"{source_key}"
        )

    @staticmethod
    def _dedupe_key(event: AstrMessageEvent, *, tag: str, ranking_mode: str) -> str:
        group_id = event.get_group_id()
        if group_id:
            scope = f"group:{group_id}"
        else:
            scope = f"private:{event.get_sender_id()}"
        source = f"search:{tag.strip().casefold()}" if tag else f"rank:{ranking_mode}"
        return f"{scope}:{source}"

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
