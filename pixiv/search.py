from __future__ import annotations

import asyncio

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes

from .downloader import cleanup


LOG_PREFIX = "[GetPx]"
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0


def _search_quality_label(value: object) -> str:
    return {
        "original": "原图",
        "large": "大图",
        "medium": "中图",
        "square_medium": "方形缩略图",
    }.get(str(value or ""), str(value or ""))


def _search_source_label(value: object) -> str:
    source = str(value or "")
    if source.startswith("lolicon"):
        return "Lolicon"
    if source.startswith("pixiv"):
        return "Pixiv"
    return source


class SearchMixin:
    """Search and source-fallback flows."""

    def _ensure_client_or_error(self, event: AstrMessageEvent) -> bool:
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            return True
        if getattr(self, "client", None) and self.client.api:
            return True
        self._init_client()
        return bool(
            (
                getattr(self, "lolicon_client", None)
                and self.lolicon_client.available
            )
            or getattr(self, "client", None)
        )

    @staticmethod
    def _event_scope(event: AstrMessageEvent) -> str:
        group_id = event.get_group_id()
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id() or ''}"

    @staticmethod
    def _source_key(tag: str, source: str) -> str:
        prefix = "search" if tag.strip() else "random"
        return f"{source}:{prefix}:{tag.strip().casefold()}" if tag.strip() else f"{source}:random"

    async def _fetch_source_candidates(
        self,
        event: AstrMessageEvent,
        tag: str,
        *,
        count: int = 20,
        offset: int = 0,
        aspect_ratio: str = "",
        use_page_cursor: bool = True,
    ) -> tuple[list[dict], int, str]:
        """优先请求 Lolicon，失败后按有无标签回退 Pixiv。"""
        lolicon_client = getattr(self, "lolicon_client", None)
        if lolicon_client and lolicon_client.available:
            try:
                if tag:
                    illusts = await lolicon_client.search(
                        tag, count=count, aspect_ratio=aspect_ratio
                    )
                    source_key = self._source_key(tag, "lolicon")
                else:
                    illusts = await lolicon_client.random(
                        count=count, aspect_ratio=aspect_ratio
                    )
                    source_key = "lolicon:random"
                if illusts:
                    return illusts, len(illusts), source_key
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} Lolicon 请求失败，尝试 Pixiv 回退: "
                    f"是否配置标签={'是' if tag else '否'} "
                    f"错误类型={type(exc).__name__}"
                )

        pixiv_source_key = self._source_key(tag, "pixiv") if tag else "pixiv:recommended"
        page_offset = offset
        if use_page_cursor and page_offset == 0 and self.image_index is not None:
            try:
                page_offset = await self.image_index.get_page_offset(
                    self._event_scope(event), pixiv_source_key
                )
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 读取 Pixiv 回退分页游标失败: "
                    f"错误类型={type(exc).__name__}"
                )

        if self.client is None:
            self._init_client()
        if self.client is None:
            return [], 0, pixiv_source_key
        try:
            if tag:
                illusts = await self.client.search(tag, offset=page_offset)
                source_key = pixiv_source_key
            else:
                illusts = await self.client.recommended(offset=page_offset)
                source_key = pixiv_source_key
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} Pixiv 回退请求失败: "
                f"是否配置标签={'是' if tag else '否'} "
                f"错误类型={type(exc).__name__}"
            )
            return [], 0, pixiv_source_key
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
        except Exception:
            pass

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
    ):
        """搜索并发送图片；Lolicon 失败时按需回退 Pixiv。"""
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

        try:
            if tag and await self._blocked_query_term(tag):
                yield event.plain_result("🚫 搜索词不符合内容安全要求")
                return
        except RuntimeError:
            yield event.plain_result("🚫 内容安全服务暂不可用，本次请求已拒绝")
            return
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        quality = self._cfg_str("image_quality", "original")
        downgrade_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB,
            0.0,
            100.0,
        )
        downgrade_limit_bytes = int(downgrade_limit_mb * 1024 * 1024)
        filter_manga = self._cfg_bool("filter_manga", True)

        # 获取作品列表：Lolicon 主源，Pixiv 搜索/推荐回退。
        illusts, raw_count, source_key = await self._fetch_source_candidates(
            event, tag, count=max_count
        )
        logger.info(
            f"{LOG_PREFIX} 搜索：标签={tag or '随机'} 来源={_search_source_label(source_key)} "
            f"请求数量={count} 画质={_search_quality_label(quality)} 返回数量={raw_count}"
        )

        if not illusts:
            yield event.plain_result("❌ 图片源请求失败或无结果，换个标签试试")
            return

        if filter_manga:
            illusts = self._filter_manga(illusts)
            if not illusts:
                yield event.plain_result(
                    "😶 过滤漫画后没有可用作品，可关闭漫画过滤后重试"
                )
                return

        try:
            illusts = await self._filter_blacklisted_illusts(illusts)
        except RuntimeError:
            yield event.plain_result("🚫 内容安全服务暂不可用，本次请求已拒绝")
            return
        if not illusts:
            yield event.plain_result(
                "😶 可用作品都被内容安全策略过滤了，换个标签后再试"
            )
            return

        pick_count = min(count, len(illusts))
        dedupe_days = self._cfg_int("dedupe_days", 1, 0, 7)
        if (
            self.image_index is not None
            and self.image_index.retention_days != dedupe_days
        ):
            try:
                await self.image_index.set_retention_days(dedupe_days)
            except Exception:
                yield event.plain_result("图片去重索引更新失败，请稍后重试")
                return

        chosen = await self._pick_illusts(
            event,
            illusts,
            pick_count,
            source_key=source_key,
            dedupe_enabled=dedupe_days > 0,
            raw_count=raw_count,
        )
        if not chosen:
            yield event.plain_result(
                "当前去重范围内没有未发送过的图片了，换个标签或稍后再试"
            )
            return
        pick_count = len(chosen)
        pending_illust_ids: set[str] = set()
        sent_illust_ids: set[str] = set()
        if dedupe_days > 0 and self.image_index is not None:
            pending_illust_ids = {
                str(illust.get("id") or "") for illust in chosen if illust.get("id")
            }

        # 判断发送模式
        send_as_forward = self._cfg_bool("send_as_forward", True)

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
                        timeout=timeout_sec,
                        downgrade_limit_bytes=downgrade_limit_bytes,
                        log_context=f"[{idx}/{pick_count}] 作品 {illust_id}",
                    )
                    logger.debug(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} "
                        f"下载完成（大小={file_size / 1024:.2f}KB，画质={_search_quality_label(actual_q)}）"
                    )
                    temp_paths.append(path)
                    downloaded.append((illust, path, actual_q, file_size))
                except asyncio.TimeoutError:
                    logger.warning(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} 下载超时 ({timeout_sec}s)"
                    )
                except Exception as e:
                    logger.debug(
                        f"{LOG_PREFIX} [{idx}/{pick_count}] 作品 {illust_id} "
                        f"下载跳过: 错误类型={type(e).__name__}"
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
                        sent_illust_ids.update(
                            str(illust.get("id") or "")
                            for illust, *_rest in downloaded
                            if illust.get("id")
                        )
                        logger.info(
                            f"{LOG_PREFIX} 合并转发 {len(nodes.nodes)} 条作品"
                            + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                        )
                        forward_success = True
                        break
                    except Exception as e:
                        if attempt < max_retries:
                            wait_sec = attempt * 2
                            logger.warning(
                                f"{LOG_PREFIX} 合并转发失败，准备重试: "
                                f"尝试次数={attempt}/{max_retries} "
                                f"等待={wait_sec}s "
                                f"错误类型={type(e).__name__}"
                            )
                            await asyncio.sleep(wait_sec)
                        else:
                            friendly_err = self._friendly_send_error(e)
                            logger.warning(
                                f"{LOG_PREFIX} 合并转发失败，降级为逐条发送: "
                                f"尝试次数={max_retries} 错误原因={friendly_err} "
                                f"错误类型={type(e).__name__}"
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
                        # 逐条发送（带重试机制）
                        for attempt in range(1, max_retries + 1):
                            try:
                                await event.send(event.chain_result(content))
                                sent_illust_ids.add(str(illust.get("id") or ""))
                                logger.info(
                                    f"{LOG_PREFIX} [降级] 作品 {illust_id} 已发送"
                                )
                                await self._record_image_usage(
                                    event,
                                    source_key,
                                    illust,
                                    feature="normal",
                                    user_id=str(event.get_sender_id() or ""),
                                )
                                break
                            except Exception as e:
                                if attempt < max_retries:
                                    await asyncio.sleep(attempt * 2)
                                else:
                                    friendly_err = self._friendly_send_error(e)
                                    logger.error(
                                        f"{LOG_PREFIX} [降级] 作品 {illust_id} "
                                        f"发送失败：尝试次数={max_retries} "
                                        f"错误原因={friendly_err} "
                                        f"错误类型={type(e).__name__}"
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
                        await self._record_image_usage(
                            event,
                            source_key,
                            illust,
                            feature="normal",
                            user_id=str(event.get_sender_id() or ""),
                        )
            else:
                # 逐条发送模式
                for illust, path, actual_q, file_size in downloaded:
                    title = illust.get("title", "无标题")
                    illust_id = illust.get("id", "?")
                    content = [
                        Plain(f"🎨 {title} (ID: {illust_id})"),
                        Image.fromFileSystem(path),
                    ]
                    # 逐条发送（带重试机制）
                    max_retries = 3
                    for attempt in range(1, max_retries + 1):
                        try:
                            await event.send(event.chain_result(content))
                            sent_illust_ids.add(str(illust.get("id") or ""))
                            logger.info(
                                f"{LOG_PREFIX} 作品 {illust_id} 已发送"
                                + (f" (第{attempt}次尝试)" if attempt > 1 else "")
                            )
                            await self._record_image_usage(
                                event,
                                source_key,
                                illust,
                                feature="normal",
                                user_id=str(event.get_sender_id() or ""),
                            )
                            break
                        except Exception as e:
                            if attempt < max_retries:
                                wait_sec = attempt * 2
                                logger.warning(
                                    f"{LOG_PREFIX} 作品 {illust_id} 发送失败，准备重试: "
                                    f"尝试次数={attempt}/{max_retries} "
                                    f"等待={wait_sec}s "
                                    f"错误类型={type(e).__name__}"
                                )
                                await asyncio.sleep(wait_sec)
                            else:
                                friendly_err = self._friendly_send_error(e)
                                logger.error(
                                    f"{LOG_PREFIX} 作品 {illust_id} 发送失败: "
                                    f"尝试次数={max_retries} 错误原因={friendly_err} "
                                    f"错误类型={type(e).__name__}"
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
                    except Exception:
                        pass
