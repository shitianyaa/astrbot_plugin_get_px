from __future__ import annotations

import asyncio
import random
import re

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes

from .downloader import cleanup


LOG_PREFIX = "[GetPx]"
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


class SearchMixin:
    """Search, ranking and illustration-detail flows."""

    def _ensure_client_or_error(self, event: AstrMessageEvent) -> bool:
        if self.client and self.client.api:
            return True
        if not self._cfg_str("pixiv_refresh_token"):
            return False
        self._init_client()
        return self.client is not None

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
        source_key = self._source_key(tag or "", ranking_mode)
        page_offset = 0
        if self.image_index is not None:
            try:
                page_offset = await self.image_index.get_page_offset(
                    self._event_scope(event), source_key
                )
            except Exception as exc:
                logger.warning(f"{LOG_PREFIX} 读取分页游标失败: {exc}")

        try:
            if tag:
                illusts = await self.client.search(tag, offset=page_offset)
            else:
                illusts = await self.client.ranking(ranking_mode, offset=page_offset)
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} Pixiv 请求失败(tag={tag!r}): {exc}")
            return [], 0, source_key
        return illusts, len(illusts), source_key

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
        except Exception as exc:
            logger.warning(f"{LOG_PREFIX} 写入当天发图索引失败: {exc}")

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
