from __future__ import annotations

import asyncio
import time

from astrbot.api import logger
from astrbot.api.all import Image, Plain
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Node, Nodes

from .constants import AIOCQHTTP_PLATFORM, MAX_IMAGE_COUNT
from .delivery import DeliveryNotSubmitted, DeliveryStatus, log_delivery_error, send_once
from .downloader import cleanup, format_log_size
from .pagination import SourceRequest, SourceResponseError, SelectionIndexError
from .safety import ContentSafetyPolicy, STRICT_CONTENT_SAFETY_POLICY
from .search_result import SearchResult


LOG_PREFIX = "[GetPx]"
DEFAULT_IMAGE_SIZE_LIMIT_MB = 10.0
SEARCH_BATCH_SIZE = 20
SEARCH_BATCH_ATTEMPTS = 3
SEARCH_DOWNLOAD_ATTEMPTS = 20
FORWARD_IMAGES_PER_NODE = 9


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

    @staticmethod
    def _should_use_forward(
        *, downloaded_count: int, threshold: int, platform_name: str
    ) -> bool:
        return platform_name == AIOCQHTTP_PLATFORM and downloaded_count > threshold

    def _p_unit_cost(self) -> int:
        return self._cfg_int("p_coin_cost", 20, 0, 200)

    async def _p_balance_error(self, user_id: str, count: int, *, unit_cost: int | None = None) -> str:
        """返回余额不足提示；余额充足或钱包不可用时返回空串。"""
        cost = self._p_unit_cost() if unit_cost is None else unit_cost
        if not user_id or cost <= 0 or count <= 0:
            return ""
        try:
            profile = await self.checkin_store.get_profile(user_id)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} /p 金币余额预检失败，已跳过余额检查: "
                f"error_type={type(exc).__name__}"
            )
            return ""
        total = cost * count
        if profile.coins < total:
            return f"金币不足，需要 {total}，当前只有 {profile.coins}。"
        return ""

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
        portrait: bool = False,
        use_page_cursor: bool = True,
        policy: ContentSafetyPolicy = STRICT_CONTENT_SAFETY_POLICY,
        request_state: SourceRequest | None = None,
    ) -> tuple[list[dict], int, str]:
        """按首选图片源顺序取图，请求失败或无结果时尝试另一源。"""
        preferred = self._cfg_str("preferred_image_source", "Lolicon").lower()
        if preferred not in ("lolicon", "pixiv"):
            logger.warning(f"{LOG_PREFIX} 首选图片源配置无效，使用默认顺序: preferred_source=lolicon")
            preferred = "lolicon"
        sources = ("pixiv", "lolicon") if preferred == "pixiv" else ("lolicon", "pixiv")
        if getattr(self, "lolicon_client", None) is None and getattr(self, "client", None) is None:
            self._init_client()
        state = request_state if request_state is not None else SourceRequest()
        state.failure_reason = ""
        source_key = ""
        for source in sources:
            source_key = self._source_key(tag, source)
            if source == "pixiv" and not tag:
                source_key = "pixiv:recommended"
            client = getattr(self, "lolicon_client" if source == "lolicon" else "client", None)
            if client is None or (source == "lolicon" and not client.available):
                logger.debug(f"{LOG_PREFIX} 图片源不可用，跳过请求: source={source}")
                continue
            try:
                if source == "lolicon":
                    if tag:
                        illusts = await client.search(
                            tag, count=count, aspect_ratio=aspect_ratio,
                            r18=0 if policy.general_only_enabled else 2,
                        )
                    else:
                        illusts = await client.random(
                            count=count, aspect_ratio=aspect_ratio,
                            r18=0 if policy.general_only_enabled else 2,
                        )
                else:
                    if source_key in state.next_params and state.next_params[source_key] is None:
                        continue
                    continuation = state.next_params.get(source_key)
                    page_offset = offset
                    if source_key not in state.next_params:
                        if use_page_cursor and page_offset == 0 and self.image_index is not None:
                            try:
                                page_offset = await self.image_index.get_page_offset(
                                    self._event_scope(event), source_key
                                )
                            except Exception as exc:
                                raise SelectionIndexError("读取分页索引失败") from exc
                    params = continuation if continuation is not None else {"offset": page_offset}
                    identity = tuple(sorted(
                        (key, tuple(value) if isinstance(value, list) else value)
                        for key, value in params.items()
                    ))
                    seen = state.seen.setdefault(source_key, set())
                    if identity in seen:
                        state.next_params[source_key] = None
                        raise SourceResponseError("repeated_continuation")
                    seen.add(identity)
                    if tag:
                        page = await client.search(
                            tag, offset=page_offset, continuation=continuation,
                            portrait=portrait,
                        )
                    else:
                        page = await client.recommended(offset=page_offset, continuation=continuation)
                    illusts = page.items
                    state.next_params[source_key] = page.next_params
                if illusts:
                    return illusts, len(illusts), source_key
                if source == "pixiv":
                    await self._advance_source_page(event, source_key, state, use_page_cursor=use_page_cursor)
                logger.info(f"{LOG_PREFIX} 图片源返回空批次: source={source}")
            except SelectionIndexError:
                raise
            except Exception as exc:
                state.failure_reason = getattr(exc, "reason", "request_failed")
                if source == "pixiv":
                    state.next_params[source_key] = None
                diagnostic = ""
                if isinstance(exc, SourceResponseError) and exc.reason == "invalid_continuation":
                    diagnostic = (
                        f" continuation_detail={exc.detail} continuation_field={exc.field}"
                    )
                logger.warning(
                    f"{LOG_PREFIX} 图片源请求失败: "
                    f"source={source} tag_configured={'yes' if tag else 'no'} "
                    f"reason={state.failure_reason} error_type={type(exc).__name__}{diagnostic}"
                )
        return [], 0, source_key

    async def _advance_source_page(
        self, event: AstrMessageEvent, source_key: str, state: SourceRequest,
        *, use_page_cursor: bool = True,
    ) -> None:
        if not use_page_cursor or self.image_index is None or not source_key.startswith("pixiv:"):
            return
        if source_key not in state.next_params:
            return
        # shortcut: 推荐完整多参数位置仅在请求内保存，跨请求仍使用数字 offset。
        params = state.next_params[source_key]
        offset = params.get("offset", 0) if params is not None else 0
        try:
            await self.image_index.set_page_offset(self._event_scope(event), source_key, offset)
        except Exception as exc:
            raise SelectionIndexError("保存分页索引失败") from exc

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
            log_delivery_error(
                exc, context=f"illust_id={illust_id}", phase="record_usage",
                delivery_status="confirmed",
            )

    async def _handle_search(
        self,
        event: AstrMessageEvent,
        tag: str,
        count_str: str,
    ):
        """命令展示层；图片发送、清理和结算由共享执行完成。"""
        result = await self._execute_search(event, tag, count_str)
        if result.message:
            yield event.plain_result(result.message)

    async def _execute_search(
        self, event: AstrMessageEvent, tag: str, count_str: str, *,
        tool: bool = False, result: SearchResult | None = None,
        selection_deadline: float | None = None, send_mode: str = "auto",
    ) -> SearchResult:
        """执行一次取图；Tool 可覆盖本次发送方式，复用命令的消费规则。"""
        result = result if result is not None else SearchResult(1)
        sender_id = str(event.get_sender_id() or "")
        unit_cost = self._p_unit_cost()
        charge_requested = unit_cost > 0 and self._cfg_bool("checkin_enabled", True)
        wallet = getattr(self, "checkin_store", None)
        charging = charge_requested and wallet is not None
        forward_threshold = self._forward_threshold()
        forward_transport = self._cfg_str("forward_transport", "file")
        if forward_transport not in ("file", "astrbot"):
            logger.warning(f"{LOG_PREFIX} 合并转发链路配置无效，使用默认链路: forward_transport=file")
            forward_transport = "file"
        if tool and send_mode == "single":
            forward_threshold = MAX_IMAGE_COUNT
        elif tool and send_mode == "forward":
            forward_threshold = 0
        # 频率限制
        wait = self._check_rate_limit(event.get_sender_id())
        if wait > 0:
            logger.debug(
                f"{LOG_PREFIX} 搜索请求触发频率限制: retry_after_seconds={wait}"
            )
            result.message = f"⏳ 请求太频繁，请 {wait} 秒后再试"
            return result

        # 参数解析
        max_count = self._cfg_int("max_count", 5, 1, MAX_IMAGE_COUNT)
        try:
            requested_count = max(1, int(count_str)) if count_str else 1
        except (TypeError, ValueError):
            requested_count = 1
        count = min(requested_count, max_count)

        result.requested_count = requested_count
        result.count = count
        reduced = False
        if tool and (not sender_id or (charge_requested and wallet is None)):
            result.message = "发图服务暂不可用，本次未执行。"
            return result
        if charging:
            if tool:
                try:
                    profile = await wallet.find_profile(sender_id)
                    affordable = (profile.coins if profile is not None else 0) // unit_cost
                except Exception as exc:
                    logger.warning(f"{LOG_PREFIX} Tool 余额读取失败，请求已拒绝: error_type={type(exc).__name__}")
                    result.message = "暂时无法确认可用余额，这次先不取图。"
                    return result
                if affordable <= 0:
                    result.message = "现在的金币不够取图，签到后再来吧。"
                    return result
                if affordable < count:
                    count = affordable
                    result.count = count
                    reduced = True
                    result.notice = f"现在的金币只够取 {count} 张，这次先取这些。"
            else:
                balance_error = await self._p_balance_error(sender_id, count, unit_cost=unit_cost)
                if balance_error:
                    result.message = balance_error
                    return result

        try:
            policy = await self._content_safety_policy(event)
            if tag and await self._blocked_query_term(tag, policy):
                result.message = "🚫 搜索词不符合内容安全要求"
                return result
        except RuntimeError:
            result.message = "🚫 内容安全服务暂不可用，本次请求已拒绝"
            return result
        timeout_sec = self._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        quality = self._cfg_str("image_quality", "original")
        size_limit_mb = self._cfg_float(
            "auto_downgrade_original_mb",
            DEFAULT_IMAGE_SIZE_LIMIT_MB,
            0.0,
            25.0,
        )
        size_limit_bytes = int(size_limit_mb * 1024 * 1024)
        filter_manga = self._cfg_bool("filter_manga", True)

        request_state = SourceRequest()
        dedupe_days = self._cfg_int("dedupe_days", 1, 0, 7)
        if (
            self.image_index is not None
            and self.image_index.retention_days != dedupe_days
        ):
            try:
                await self.image_index.set_retention_days(dedupe_days)
            except Exception:
                result.message = "图片去重索引更新失败，请稍后重试"
                return result

        pending_illust_ids: set[str] = set()
        sent_illust_ids: set[str] = set()
        unknown_illust_ids: set[str] = set()
        retained_paths: set[str] = set()
        file_transport = (
            event.get_platform_name() == AIOCQHTTP_PLATFORM
            and getattr(event, "bot", None) is not None
        )
        # 下载所有图片
        downloaded: list[tuple[dict, str, str, int]] = []
        temp_paths: list[str] = []
        image_sources: dict[str, str] = {}
        attempted_ids: set[str] = set()
        batch_attempts = 0
        download_attempts = 0
        illusts: list[dict] = []
        source_key = ""
        queue: list[dict] = []
        deadline: float | None = selection_deadline
        failure_message = "❌ 图片源请求失败或无结果，换个标签试试"

        async def deliver_images(
            content, images, *, context: str, forward_transport: str = "file"
        ) -> None:
            """按实际提交阶段维护计费、去重占用和文件读取窗口。"""
            ids = {str(illust["id"]) for illust, *_ in images if illust.get("id")}

            def retain_unknown():
                unknown_illust_ids.update(ids)
                if file_transport:
                    retained_paths.update(path for _, path, *_ in images)

            try:
                status = await send_once(
                    event, content, context=context, forward_transport=forward_transport
                )
            except DeliveryNotSubmitted:
                raise
            except asyncio.CancelledError:
                # 取消发生在提交期间也可能已送达，保留占用与协议端读取窗口。
                retain_unknown()
                result.record_delivery(DeliveryStatus.UNKNOWN, len(images))
                raise
            result.record_delivery(status, len(images))
            if status is DeliveryStatus.UNKNOWN:
                retain_unknown()
            if status in (DeliveryStatus.CONFIRMED, DeliveryStatus.ACCEPTED, DeliveryStatus.TIMED_OUT):
                if status in (DeliveryStatus.ACCEPTED, DeliveryStatus.TIMED_OUT) and file_transport:
                    retained_paths.update(path for _, path, *_ in images)
                sent_illust_ids.update(ids)
                for illust, *_ in images:
                    await self._record_image_usage(
                        event, image_sources[str(illust.get("id") or "")], illust, feature="normal",
                        user_id=str(event.get_sender_id() or ""),
                    )

        try:
            while len(downloaded) < count and download_attempts < SEARCH_DOWNLOAD_ATTEMPTS:
                if deadline is not None and time.monotonic() >= deadline:
                    logger.info(f"{LOG_PREFIX} 发图取图总时间预算耗尽: downloaded_count={len(downloaded)}")
                    break
                if not queue:
                    remaining = [item for item in illusts if str(item.get("id") or "") not in attempted_ids]
                    if remaining:
                        try:
                            queue = await self._pick_illusts(
                                event, remaining, min(count - len(downloaded), len(remaining)),
                                source_key=source_key, dedupe_enabled=dedupe_days > 0,
                            )
                        except SelectionIndexError as exc:
                            logger.warning(f"{LOG_PREFIX} 发图去重服务不可用: error_type={type(exc).__name__}")
                            failure_message = "图片去重服务暂不可用，请稍后重试"
                            break
                        if not queue:
                            failure_message = "当前去重范围内没有未发送过的图片了，换个标签或稍后再试"
                    if not queue:
                        try:
                            await self._advance_source_page(event, source_key, request_state)
                            if batch_attempts >= SEARCH_BATCH_ATTEMPTS:
                                break
                            fetch = self._fetch_source_candidates(
                                event, tag, count=SEARCH_BATCH_SIZE, policy=policy,
                                request_state=request_state,
                            )
                            if deadline is None:
                                illusts, raw_count, source_key = await fetch
                                deadline = time.monotonic() + timeout_sec * max(3, count)
                            else:
                                illusts, raw_count, source_key = await asyncio.wait_for(
                                    fetch, timeout=max(0, deadline - time.monotonic()),
                                )
                            if batch_attempts == 0:
                                deadline = min(deadline, time.monotonic() + timeout_sec * max(3, count))
                            batch_attempts += 1
                            logger.info(
                                f"{LOG_PREFIX} 图源候选批次已返回: source={_search_source_label(source_key)} "
                                f"batch={batch_attempts}/{SEARCH_BATCH_ATTEMPTS} raw_candidate_count={raw_count}"
                            )
                            if not illusts:
                                if request_state.failure_reason:
                                    failure_message = "❌ 图片源暂不可用，请稍后重试"
                                continue
                            if filter_manga:
                                illusts = self._filter_manga(illusts)
                                if not illusts:
                                    failure_message = "😶 过滤漫画后没有可用作品，换个标签后再试"
                                    continue
                            illusts = await self._filter_blacklisted_illusts(illusts, policy)
                            if not illusts:
                                failure_message = "😶 可用作品都被内容安全策略过滤了，换个标签后再试"
                        except SelectionIndexError as exc:
                            logger.warning(f"{LOG_PREFIX} 发图分页索引不可用: error_type={type(exc).__name__}")
                            failure_message = "图片去重服务暂不可用，请稍后重试"
                            break
                        except RuntimeError as exc:
                            logger.warning(f"{LOG_PREFIX} 发图候选处理失败: error_type={type(exc).__name__}")
                            failure_message = "🚫 内容安全或图片源服务暂不可用，请稍后重试"
                            break
                        except Exception as exc:
                            logger.warning(f"{LOG_PREFIX} 发图候选补取失败: error_type={type(exc).__name__}")
                            break
                        continue
                    for item in queue:
                        item_id = str(item.get("id") or "")
                        image_sources[item_id] = source_key
                        if dedupe_days > 0 and self.image_index is not None and item_id:
                            pending_illust_ids.add(item_id)
                illust = queue.pop(0)
                illust_id = illust.get("id", "?")
                item_id = str(illust.get("id") or "")
                if item_id in attempted_ids:
                    continue
                attempted_ids.add(item_id)
                download_attempts += 1
                failure_message = "😢 所有图片均下载失败，请稍后再试"
                idx = download_attempts
                download_ready = False

                try:
                    path, actual_q, file_size = await asyncio.wait_for(
                        self.downloader.download_for_send(
                            illust, quality, timeout=timeout_sec,
                            size_limit_bytes=size_limit_bytes,
                            log_context=f"[下载尝试 {idx}/{SEARCH_DOWNLOAD_ATTEMPTS}] 作品 {illust_id}",
                            allow_quality_fallback=False,
                        ),
                        timeout=max(0, deadline - time.monotonic()),
                    )
                    logger.debug(
                        f"{LOG_PREFIX} 发图候选下载完成: illust_id={illust_id} "
                        f"download_attempt={idx}/{SEARCH_DOWNLOAD_ATTEMPTS} size={format_log_size(file_size)} "
                        f"quality={_search_quality_label(actual_q)}"
                    )
                    temp_paths.append(path)
                    downloaded.append((illust, path, actual_q, file_size))
                    result.downloaded_count = len(downloaded)
                    download_ready = True
                except asyncio.TimeoutError:
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} "
                        f"download_attempt={idx}/{SEARCH_DOWNLOAD_ATTEMPTS} reason=timeout "
                        f"timeout_seconds={timeout_sec}"
                    )
                except Exception as e:
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} "
                        f"download_attempt={idx}/{SEARCH_DOWNLOAD_ATTEMPTS} reason=download_error "
                        f"error_type={type(e).__name__}"
                    )
                finally:
                    item_id = str(illust.get("id") or "")
                    if not download_ready and self.image_index is not None and item_id in pending_illust_ids:
                        try:
                            await self.image_index.release_usage(
                                scope=self._event_scope(event),
                                source_key=image_sources[item_id],
                                illust_id=item_id, feature="normal_pending",
                            )
                            pending_illust_ids.discard(item_id)
                        except Exception as exc:
                            log_delivery_error(
                                exc, context=f"illust_id={item_id}", phase="release_usage",
                                delivery_status="not_submitted",
                            )

            # 统一发送（避免 yield 和 send 混用导致消息拆分）
            if not downloaded:
                result.message = failure_message
                return result

            # 非 OneBot 平台不支持合并转发，自动降级。
            use_forward = self._should_use_forward(
                downloaded_count=len(downloaded),
                threshold=forward_threshold,
                platform_name=event.get_platform_name(),
            )

            if use_forward:
                # 合并转发模式：所有图片打包成一条聊天记录
                raw_self_id = event.get_self_id()
                # AstrBot 的 Node.uin 是字符串，且部分平台的 self_id 不是数字。
                self_id = "0" if raw_self_id is None else str(raw_self_id)
                nodes = Nodes([])
                # 多图共用节点，减少协议端逐节点生成消息的次数。
                for start in range(0, len(downloaded), FORWARD_IMAGES_PER_NODE):
                    content = []
                    for illust, path, _actual_q, _file_size in downloaded[
                        start:start + FORWARD_IMAGES_PER_NODE
                    ]:
                        title = illust.get("title", "无标题")
                        illust_id = illust.get("id", "?")
                        content.extend([
                            Plain(f"🎨 {title} (ID: {illust_id})"),
                            Image.fromFileSystem(path),
                        ])
                    nodes.nodes.append(Node(uin=self_id, name="Pixiv", content=content))
                # 缺图说明并入最后节点，不再为纯文本多建一个节点。
                failed_count = count - len(downloaded)
                if failed_count > 0:
                    nodes.nodes[-1].content.append(
                        Plain(f"⚠️ 少 {failed_count} 张符合画质和大小限制的可用图片，已跳过")
                    )
                await deliver_images(
                    [nodes], downloaded, context="send_mode=forward",
                    forward_transport=forward_transport,
                )
            else:
                for illust, path, actual_q, file_size in downloaded:
                    illust_id = illust.get("id", "?")
                    content = [
                        Plain(f"🎨 {illust.get('title', '无标题')} (ID: {illust_id})"),
                        Image.fromFileSystem(path),
                    ]
                    await deliver_images(
                        content, [(illust, path, actual_q, file_size)],
                        context=f"send_mode=single illust_id={illust_id}",
                    )
        except asyncio.CancelledError:
            result.interrupted = True
            raise
        finally:
            cleanup_task = asyncio.create_task(self._finish_search(
                event, result, wallet=wallet, charging=charging, unit_cost=unit_cost,
                sender_id=sender_id, tool=tool, reduced=reduced,
                sent_illust_ids=sent_illust_ids, temp_paths=temp_paths,
                retained_paths=retained_paths, pending_illust_ids=pending_illust_ids,
                unknown_illust_ids=unknown_illust_ids, image_sources=image_sources,
            ))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(cleanup_task)
                    break
                except asyncio.CancelledError:
                    result.interrupted = True
                    cancelled = True
                    if cleanup_task.cancelled():
                        raise
            if cancelled:
                raise asyncio.CancelledError
        return result

    async def _finish_search(
        self, event, outcome, *, wallet, charging, unit_cost, sender_id, tool, reduced,
        sent_illust_ids, temp_paths, retained_paths, pending_illust_ids,
        unknown_illust_ids, image_sources,
    ):
        try:
            if charging and sent_illust_ids:
                total_cost = unit_cost * len(sent_illust_ids)
                outcome.settlement = "unknown"
                try:
                    result = await wallet.spend_coins(
                        user_id=sender_id,
                        cost=total_cost,
                    )
                    if not result.success:
                        outcome.settlement = "failed"
                        if tool:
                            outcome.message = "图片发送已尝试，但金币结算未完成，请联系管理员核对；不要重复取图。"
                        logger.warning(
                            f"{LOG_PREFIX} /p 金币结算未完成: "
                            f"sent_count={len(sent_illust_ids)} cost={total_cost} "
                            f"result_message={result.message}"
                        )
                        try:
                            if not tool:
                                await event.send(event.plain_result(f"⚠️ 金币结算未完成：{result.message}"))
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 发送金币结算警告失败: "
                                f"error_type={type(exc).__name__}"
                            )
                    else:
                        outcome.settlement = "completed"
                        # spend_coins 返回事务内的扣款后余额；反推同事务的扣款前余额。
                        after = result.profile.coins // unit_cost
                        before = (result.profile.coins + total_cost) // unit_cost
                        if tool and not reduced and before > 2 and after <= 2:
                            outcome.notice = "剩下的金币快不够取图了，记得签到补充一下。"
                        logger.info(
                            f"{LOG_PREFIX} /p 金币结算完成: "
                            f"sent_count={len(sent_illust_ids)} cost={total_cost}"
                        )
                        try:
                            if not tool:
                                await event.send(
                                    event.plain_result(
                                        f"💰 本次发图消耗 {total_cost} 金币"
                                        f"（{len(sent_illust_ids)} 张），余额 {result.profile.coins}。"
                                    )
                                )
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 发送金币结算提醒失败: "
                                f"error_type={type(exc).__name__}"
                            )
                except Exception as exc:
                    if tool:
                        outcome.message = "金币结算状态暂时无法确认，请联系管理员核对；不要重复取图。"
                    logger.warning(
                        f"{LOG_PREFIX} /p 金币结算调用异常，扣费状态待核对: "
                        f"error_type={type(exc).__name__}"
                    )
        finally:
            for p in temp_paths:
                if p in retained_paths:
                    self.downloader.defer_cleanup(p)
                else:
                    cleanup(p)
            if pending_illust_ids and self.image_index is not None:
                for illust_id in pending_illust_ids - sent_illust_ids - unknown_illust_ids:
                    try:
                        await self.image_index.release_usage(
                            scope=self._event_scope(event),
                            source_key=image_sources[illust_id],
                            illust_id=illust_id,
                            feature="normal_pending",
                        )
                    except Exception as exc:
                        log_delivery_error(
                            exc, context=f"illust_id={illust_id}", phase="release_usage",
                            delivery_status="not_confirmed",
                        )
