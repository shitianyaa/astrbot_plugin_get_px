"""AstrBot 插件 — Pixiv 发图

通过标签搜索 Pixiv 插画并发送图片，支持排行榜、R18 过滤、多页作品、代理配置。

搜索指令：
    /p [标签] [数量]           搜索并发送图片

管理指令：
    /pr [排行类型] [数量]      获取排行榜
    /prl                       查看所有排行榜类型
    /pi <作品ID>               查看作品详情
    /ph                        查看帮助
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import tempfile
import time
from dataclasses import dataclass, field

import aiohttp

from astrbot.api.all import AstrBotConfig, Image, Plain, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes
from astrbot.api.star import Context, Star

# ──────────────────────────────────────────────────────────────────────
# 常量
# ──────────────────────────────────────────────────────────────────────

LOG_PREFIX = "[GetPx]"

RANKING_MODES = {
    "day":           "今日",
    "week":          "本周",
    "month":         "本月",
    "day_male":      "男性向",
    "day_female":    "女性向",
    "week_original": "原创",
    "week_rookie":   "新人",
    "day_manga":     "漫画",
}

PIXIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.pixiv.net/",
}


# ──────────────────────────────────────────────────────────────────────
# Pixiv API 客户端
# ──────────────────────────────────────────────────────────────────────

@dataclass
class PixivClient:
    """封装 pixivpy-async 的登录与缓存逻辑。"""

    refresh_token: str
    proxy: str = ""

    _api: object = field(default=None, repr=False)
    _cached_token: str = field(default="", repr=False)
    _expires_at: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ── 登录 ──

    async def ensure_logged_in(self):
        """确保 API 已登录，带 50 分钟自动刷新。"""
        async with self._lock:
            now = time.monotonic()
            if self._api is not None and self._cached_token == self.refresh_token and now < self._expires_at:
                return

            try:
                from pixivpy_async import AppPixivAPI
            except ImportError:
                raise RuntimeError("未安装 pixivpy-async，请运行: pip install pixivpy-async")

            api = AppPixivAPI(proxy=self.proxy) if self.proxy else AppPixivAPI()
            await api.login(refresh_token=self.refresh_token)

            self._api = api
            self._cached_token = self.refresh_token
            self._expires_at = now + 3000  # 50 分钟
            logger.info(f"{LOG_PREFIX} Pixiv 登录成功")

    @property
    def api(self):
        return self._api

    # ── 搜索 ──

    async def search(self, tag: str) -> list[dict]:
        """按标签搜索插画。"""
        await self.ensure_logged_in()
        resp = await self._api.search_illust(tag, search_target="partial_match_for_tags", sort="date_desc")
        return list(resp.get("illusts") or [])

    async def ranking(self, mode: str = "week") -> list[dict]:
        """获取排行榜。"""
        await self.ensure_logged_in()
        resp = await self._api.illust_ranking(mode=mode)
        return list(resp.get("illusts") or [])

    async def illust_detail(self, illust_id: int) -> dict | None:
        """获取单个作品详情。"""
        await self.ensure_logged_in()
        try:
            resp = await self._api.illust_detail(illust_id)
            return resp.get("illust")
        except Exception:
            return None

    # ── 关闭 ──

    async def close(self):
        if self._api is not None:
            close = getattr(self._api, "close", None)
            if callable(close):
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    pass
            self._api = None


# ──────────────────────────────────────────────────────────────────────
# 插件主类
# ──────────────────────────────────────────────────────────────────────

class GetPxPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client: PixivClient | None = None
        self.session: aiohttp.ClientSession | None = None
        self._last_request: dict[str, float] = {}  # 用户频率限制

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
        if self.session is not None and not self.session.closed:
            await self.session.close()
            self.session = None
        self._last_request.clear()
        logger.info(f"{LOG_PREFIX} 插件已停止")

    # ──────────────────────────────────────────────────────────────
    # 指令：搜索（主指令）
    # ──────────────────────────────────────────────────────────────

    @filter.command("p")
    async def cmd_p(self, event: AstrMessageEvent, tag: str = "", count: str = ""):
        """搜索 Pixiv 并发送图片。用法: /p [标签] [数量]"""
        if not self._ensure_client_or_error(event):
            yield event.plain_result("⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token")
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
            yield event.plain_result("⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token")
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
            yield event.plain_result("⚠️ 未配置 Pixiv Token，请在插件设置中填写 pixiv_refresh_token")
            return
        if not illust_id or not illust_id.isdigit():
            yield event.plain_result("⚠️ 用法: /pi <作品ID>\n示例: /pi 12345678")
            return
        event.stop_event()
        async for result in self._handle_info(event, int(illust_id)):
            yield result

    @filter.command("ph")
    async def cmd_help(self, event: AstrMessageEvent):
        """查看 Pixiv 插件帮助。"""
        event.stop_event()
        yield event.plain_result(self._build_help())

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

    async def _handle_search(self, event: AstrMessageEvent, tag: str, count_str: str, *, ranking_override: str = ""):
        """搜索并发送图片。ranking_override 非空时覆盖配置中的排行榜类型。"""
        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
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
        ai_enabled = self._cfg_bool("ai_enabled", False)
        ai_prob = self._cfg_int("ai_probability", 30, 0, 100)
        ai_max = self._cfg_int("ai_max_images", 3, 1, 20)
        ai_pre_msg = self._cfg_str("ai_pre_message", "让我先品鉴一番，你稍等喵~")
        ai_vision_pid = self._cfg_str("ai_vision_provider_id", "")
        ai_comment_pid = self._cfg_str("ai_comment_provider_id", "")
        ai_vision_prompt = self._cfg_str("ai_vision_prompt", "请详细描述这张插画的内容，包括画风、构图、配色、角色特征、表情、姿势、背景等。用简洁的中文描述。")
        ai_comment_prompt = self._cfg_str("ai_comment_prompt", "你是一个 Pixiv 插画鉴赏专家。根据以下图片描述，用轻松有趣的语气写一句简短评论（50字以内）。\n\n图片描述：{description}")

        if ranking_mode not in RANKING_MODES:
            ranking_mode = "week"

        # 获取作品列表
        logger.info(f"{LOG_PREFIX} 搜索: tag={tag!r} rank={ranking_mode} count={count} quality={quality}")
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
                yield event.plain_result("🔒 过滤后没有可用作品。如果目标内容包含敏感作品，请到 Pixiv 官网「设置 > 显示设置」中开启「显示作品」选项，然后将插件 R18 设置改为 2（混合）。")
            elif r18_mode == 1:
                yield event.plain_result("🔒 没有找到 R18 作品。请确认你的 Pixiv 账号已在官网「显示设置」中开启了「显示敏感作品」和「显示 R-18 作品」。")
            else:
                yield event.plain_result("🔒 过滤后没有可用作品")
            return

        # 随机选取
        pick_count = min(count, len(illusts))
        chosen = random.sample(illusts, pick_count)

        # 判断发送模式
        send_as_forward = self._cfg_bool("send_as_forward", True)

        # 下载所有图片
        downloaded: list[tuple[dict, str]] = []  # (illust, file_path)
        temp_paths: list[str] = []
        try:
            for idx, illust in enumerate(chosen, 1):
                illust_id = illust.get("id", "?")
                title = illust.get("title", "无标题")

                url = self._pick_image_url(illust, quality)
                if not url:
                    logger.warning(f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} 「{title}」无可下载 URL")
                    continue

                actual_q = "original" if "original" in url else "large" if "large" in url else "medium" if "medium" in url else "square_medium"
                logger.info(f"{LOG_PREFIX} [{idx}/{pick_count}] 下载作品 {illust_id} 「{title}」 quality={actual_q}")

                try:
                    path = await self._download_image(url, proxy=self._cfg_str("pixiv_proxy_url"), timeout=timeout_sec)
                    file_size = os.path.getsize(path)
                    logger.info(f"{LOG_PREFIX} [{idx}/{pick_count}] 下载完成 {illust_id} -> {path} ({file_size / 1024:.1f} KB)")
                    temp_paths.append(path)
                    downloaded.append((illust, path))
                except asyncio.TimeoutError:
                    logger.warning(f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} 下载超时 ({timeout_sec}s)")
                except Exception as e:
                    logger.error(f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} 下载失败: {e}")

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
                        logger.info(f"{LOG_PREFIX} [AI] 共 {len(downloaded)} 张图，仅分析前 {ai_max} 张")
                    async def _analyze(idx: int, illust: dict, path: str):
                        try:
                            comment = await self._ai_comment(event, path, ai_vision_pid, ai_comment_pid, ai_vision_prompt, ai_comment_prompt)
                            if comment:
                                ai_comments[illust.get("id", 0)] = comment
                                logger.info(f"{LOG_PREFIX} [AI] 作品 {illust.get('id')} 评论完成: {comment[:40]}...")
                        except Exception as e:
                            illust_id = illust.get("id", 0)
                            logger.warning(f"{LOG_PREFIX} [AI] 作品 {illust_id} 识图失败: {e}，已降级跳过")
                            ai_comments[illust_id] = "羞死啦 羞死啦 ~"
                    await asyncio.gather(*[_analyze(i, il, p) for i, (il, p) in enumerate(to_analyze)])

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
                    nodes.nodes.append(Node(
                        uin=self_id,
                        name="Pixiv",
                        content=content,
                    ))
                await event.send(event.chain_result([nodes]))
                logger.info(f"{LOG_PREFIX} 合并转发 {len(nodes.nodes)} 条作品")
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
                        content.append(Plain(f"🤖 {comment}"))
                    await event.send(event.chain_result(content))
        finally:
            for p in temp_paths:
                self._cleanup(p)

    async def _handle_rank(self, event: AstrMessageEvent, mode: str, count_str: str = ""):
        """排行榜模式。"""
        mode = mode.lower().strip() if mode else "week"

        if mode not in RANKING_MODES:
            yield event.plain_result(f"⚠️ 未知排行榜类型: {mode}\n发送 /prl 查看所有类型")
            return

        # 走搜索逻辑，通过参数传递排行榜类型
        async for result in self._handle_search(event, tag="", count_str=count_str, ranking_override=mode):
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
            "❓ /ph 显示本帮助",
        ]
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────
    # AI 识图
    # ──────────────────────────────────────────────────────────────

    async def _ai_comment(self, event: AstrMessageEvent, image_path: str,
                           vision_pid: str, comment_pid: str,
                           vision_prompt: str, comment_prompt: str) -> str:
        """两步 AI 识图：1. 视觉模型描述图片 → 2. 文本模型评论。"""
        from pathlib import Path
        image_url = Path(image_path).as_uri()
        umo = event.unified_msg_origin

        # ── 解析识图模型 ──
        v_pid = await self._resolve_provider(vision_pid, umo, prefer_vision=True)
        if not v_pid:
            raise RuntimeError("未配置视觉模型，无法进行 AI 识图")
        logger.info(f"{LOG_PREFIX} [AI] 识图模型: {v_pid}")

        # 第一步：视觉模型识图
        vision_resp = await self.context.llm_generate(
            chat_provider_id=v_pid,
            prompt=vision_prompt,
            image_urls=[image_url],
        )
        description = (vision_resp.completion_text or "").strip()
        if not description:
            raise RuntimeError("视觉模型返回空结果")
        logger.info(f"{LOG_PREFIX} [AI] 识图结果: {description[:80]}...")

        # ── 解析评论模型 ──
        c_pid = await self._resolve_provider(comment_pid, umo)
        if not c_pid:
            raise RuntimeError("未配置评论模型")
        logger.info(f"{LOG_PREFIX} [AI] 评论模型: {c_pid}")

        # 第二步：文本模型评论
        final_prompt = comment_prompt.replace("{description}", description)
        comment_resp = await self.context.llm_generate(
            chat_provider_id=c_pid,
            prompt=final_prompt,
        )
        return (comment_resp.completion_text or "").strip()

    async def _resolve_provider(self, config_pid: str, umo: str, prefer_vision: bool = False) -> str:
        """解析 provider ID。优先级：配置 > 框架全局视觉模型 > 当前会话模型。"""
        if config_pid:
            return config_pid
        if prefer_vision:
            # 尝试框架全局图片描述模型
            try:
                cfg = self.context.get_config()
                vlm_id = str((cfg.get("provider_settings") or {}).get("default_image_caption_provider_id", "") or "").strip()
                if vlm_id:
                    return vlm_id
            except Exception:
                pass
        # 回退到当前会话模型
        try:
            pid = await self.context.get_current_chat_provider_id(umo=umo)
            if pid:
                return str(pid).strip()
        except Exception:
            pass
        return ""

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
    def _pick_image_url(illust: dict, quality: str = "original") -> str:
        """从作品数据中提取图片 URL。quality: original / large / medium。"""
        # 质量优先级
        quality_order = {
            "original": ["original", "large", "medium", "square_medium"],
            "large":    ["large", "medium", "square_medium", "original"],
            "medium":   ["medium", "square_medium", "large", "original"],
        }
        order = quality_order.get(quality, quality_order["original"])

        # 单页作品
        meta_single = illust.get("meta_single_page") or {}
        image_urls = illust.get("image_urls") or {}
        for q in order:
            if q == "original":
                url = meta_single.get("original_image_url")
                if url:
                    return url
            else:
                if image_urls.get(q):
                    return image_urls[q]

        # 多页作品：取第一页
        meta_pages = illust.get("meta_pages") or []
        if meta_pages:
            first_urls = (meta_pages[0] or {}).get("image_urls") or {}
            for q in order:
                if first_urls.get(q):
                    return first_urls[q]

        return ""

    async def _download_image(self, url: str, proxy: str, timeout: float) -> str:
        """下载图片到临时文件，返回路径。"""
        session = self._ensure_session()
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        async with session.get(url, headers=PIXIV_HEADERS, proxy=proxy or None, timeout=client_timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            body = await resp.read()

        # 根据 URL 判断后缀
        suffix = ".jpg"
        lower_path = url.split("?")[0].lower()
        for ext in (".png", ".gif", ".webp", ".jpeg"):
            if lower_path.endswith(ext):
                suffix = ext
                break

        fd, path = tempfile.mkstemp(prefix="get_px_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return path

    def _ensure_session(self) -> aiohttp.ClientSession:
        """确保全局 aiohttp 会话存在。"""
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    @staticmethod
    def _cleanup(path: str):
        """安全删除临时文件。"""
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

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
