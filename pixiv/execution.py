"""一次取图请求的选图、下载、发送与结算状态。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from astrbot.api import logger
from astrbot.api.all import Image, Plain
from astrbot.api.message_components import Node, Nodes

from .constants import AIOCQHTTP_PLATFORM, MAX_IMAGE_COUNT
from .delivery import (
    DeliveryNotSubmitted,
    DeliveryStatus,
    log_delivery_error,
    send_once,
)
from .downloader import cleanup
from .search_result import SearchResult
from .selection import ImageSelection

LOG_PREFIX = "[GetPx]"
DEFAULT_IMAGE_SIZE_LIMIT_MB = 10.0
SEARCH_BATCH_SIZE = 20
SEARCH_BATCH_ATTEMPTS = 3
SEARCH_DOWNLOAD_ATTEMPTS = 20


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


class SearchExecution:
    """状态仅属于本次请求，命令与模型工具共用同一执行链。"""

    def __init__(
        self,
        plugin,
        event,
        tag,
        count_str,
        *,
        tool=False,
        result=None,
        selection_deadline=None,
        send_mode="auto",
        selection=None,
        candidate_stream=None,
    ):
        self.plugin = plugin
        self.event = event
        self.tag = tag
        self.count_str = count_str
        self.tool = tool
        self.result = result
        self.selection_deadline = selection_deadline
        self.send_mode = send_mode
        self.selection = selection
        self.candidate_stream = candidate_stream

    async def run(self) -> SearchResult:
        if not await self._prepare() or not await self._load_candidates():
            return self.result
        try:
            if not await self._download_images():
                return self.result
            if not self.downloaded:
                self.result.message = self.failure_message
                return self.result
            if self.tool and len(self.downloaded) < self.count:
                self.result.message = f"本次只找到 {len(self.downloaded)} 张可用图片（原需 {self.count} 张），其余已跳过；未放宽筛选条件。"
            await self._send_images()
        except asyncio.CancelledError:
            self.result.interrupted = True
            raise
        finally:
            await self._finish()
        return self.result

    async def _prepare(self):
        """校验数量、余额和安全策略，准备本次下载配置。"""
        self.selection = (
            self.selection
            if self.tool and self.selection is not None
            else ImageSelection()
        )
        self.result = self.result if self.result is not None else SearchResult(1)
        self.sender_id = str(self.event.get_sender_id() or "")
        self.unit_cost = self.plugin._p_unit_cost()
        charge_requested = self.unit_cost > 0 and self.plugin._cfg_bool(
            "checkin_enabled", True
        )
        self.wallet = getattr(self.plugin, "checkin_store", None)
        self.charging = charge_requested and self.wallet is not None
        self.max_count = self.plugin._cfg_int("max_count", 5, 1, MAX_IMAGE_COUNT)
        self.result.max_count = self.max_count
        self.forward_threshold = self.plugin._forward_threshold()
        if self.tool and self.send_mode == "single":
            self.forward_threshold = MAX_IMAGE_COUNT
        elif self.tool and self.send_mode == "forward":
            self.forward_threshold = 0
        wait = self.plugin._check_rate_limit(self.event.get_sender_id())
        if wait > 0:
            logger.debug(
                f"{LOG_PREFIX} 搜索请求触发频率限制: retry_after_seconds={wait}"
            )
            self.result.message = (
                f"请求触发频率限制，还需等待 {wait} 秒，本次未开始取图。"
                if self.tool
                else f"⏳ 请求太频繁，请 {wait} 秒后再试"
            )
            return False
        try:
            requested_count = max(1, int(self.count_str)) if self.count_str else 1
        except (TypeError, ValueError):
            requested_count = 1
        self.count = min(requested_count, self.max_count)
        self.result.requested_count = requested_count
        self.result.count = self.count
        if self.tool and self.count < requested_count:
            self.result.count_limits.append("max_count")
            self.result.notice = (
                f"本次请求 {requested_count} 张，插件单次上限为 {self.max_count} 张。"
            )
        if self.tool and (
            not self.sender_id or (charge_requested and self.wallet is None)
        ):
            self.result.message = "发图服务暂不可用，本次未执行。"
            return False
        if self.charging:
            if self.tool:
                try:
                    profile = await self.wallet.find_profile(self.sender_id)
                    affordable = (
                        profile.coins if profile is not None else 0
                    ) // self.unit_cost
                except Exception as exc:
                    logger.warning(
                        f"{LOG_PREFIX} Tool 余额读取失败，请求已拒绝: error_type={type(exc).__name__}"
                    )
                    self.result.message = "暂时无法确认可用余额，这次先不取图。"
                    return False
                if affordable <= 0:
                    self.result.message = "现在的金币不够取图，签到后再来吧。"
                    return False
                if affordable < self.count:
                    self.count = affordable
                    self.result.count = self.count
                    self.result.count_limits.append("balance")
                    self.result.notice += (
                        f"现在的金币只够取 {self.count} 张，这次先取这些。"
                    )
            else:
                balance_error = await self.plugin._p_balance_error(
                    self.sender_id, self.count, unit_cost=self.unit_cost
                )
                if balance_error:
                    self.result.message = balance_error
                    return False
        try:
            self.policy = await self.plugin._content_safety_policy(self.event)
            if self.tag and await self.plugin._blocked_query_term(
                self.tag, self.policy
            ):
                self.result.message = "🚫 搜索词不符合内容安全要求"
                return False
        except RuntimeError:
            self.result.message = "🚫 内容安全服务暂不可用，本次请求已拒绝"
            return False
        self.timeout_sec = self.plugin._cfg_float("request_timeout", 30.0, 5.0, 120.0)
        self.quality = self.plugin._cfg_str("image_quality", "original")
        size_limit_mb = self.plugin._cfg_float(
            "auto_downgrade_original_mb", DEFAULT_IMAGE_SIZE_LIMIT_MB, 0.0, 25.0
        )
        self.size_limit_bytes = self.selection.size_limit_bytes(
            int(size_limit_mb * 1024 * 1024)
        )
        self.filter_manga = self.plugin._cfg_bool("filter_manga", True)
        return True

    async def _load_candidates(self):
        """取得首批候选并初始化本次占用、文件和预算状态。"""
        if self.candidate_stream is not None:
            self.candidate_stream.policy = self.policy
            if self.candidate_stream.query.mode == "artwork":
                omitted = self.candidate_stream.query.targets[self.count :]
                self.candidate_stream.query = replace(
                    self.candidate_stream.query,
                    targets=self.candidate_stream.query.targets[: self.count],
                )
                self.result.targets = self.candidate_stream.target_results
                self.result.targets.extend(
                    (target.summary("count_limited") for target in omitted)
                )
        fetch = self._fetch_batch()
        if self.selection_deadline is None:
            self.illusts, self.raw_count, self.source_key = await fetch
        else:
            try:
                self.illusts, self.raw_count, self.source_key = await asyncio.wait_for(
                    fetch, timeout=max(0, self.selection_deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                self.result.message = "本次取图等待时间已用尽，未开始发送。"
                return False
        logger.info(
            f"{LOG_PREFIX}"
            f" 搜索候选获取完成: tag_configured={('yes' if self.tag else 'no')}"
            f" source={_search_source_label(self.source_key)}"
            f" requested_count={self.result.requested_count}"
            f" effective_count={self.count}"
            f" max_count={self.max_count}"
            f" quality={_search_quality_label(self.quality)}"
            f" candidate_count={self.raw_count}"
        )
        if not self.illusts and self.candidate_stream is None:
            self.result.message = (
                "图片源请求失败或未返回候选，本次未开始发送。"
                if self.tool
                else "❌ 图片源请求失败或无结果，换个标签试试"
            )
            return False
        if self.filter_manga:
            self.illusts = self.plugin._filter_manga(self.illusts)
            if not self.illusts and self.candidate_stream is None:
                self.result.message = (
                    "没有通过漫画过滤的可用作品，本次未开始发送。"
                    if self.tool
                    else "😶 过滤漫画后没有可用作品，可关闭漫画过滤后重试"
                )
                return False
        try:
            self.illusts = await self.plugin._filter_blacklisted_illusts(
                self.illusts, self.policy
            )
        except RuntimeError:
            self.result.message = "🚫 内容安全服务暂不可用，本次请求已拒绝"
            return False
        if not self.illusts and self.candidate_stream is None:
            self.result.message = (
                "没有通过内容安全检查的可用作品，本次未开始发送。"
                if self.tool
                else "😶 可用作品都被内容安全策略过滤了，换个标签后再试"
            )
            return False
        self.illusts = [
            item
            for item in self.illusts
            if self.candidate_stream is not None
            or self.selection.matches_dimensions(item.get("width"), item.get("height"))
        ]
        self.pick_count = min(self.count, len(self.illusts))
        self.dedupe_days = self.plugin._cfg_int("dedupe_days", 1, 0, 7)
        if (
            self.plugin.image_index is not None
            and self.plugin.image_index.retention_days != self.dedupe_days
        ):
            try:
                await self.plugin.image_index.set_retention_days(self.dedupe_days)
            except Exception:
                self.result.message = (
                    "图片去重索引更新失败，本次未开始发送。"
                    if self.tool
                    else "图片去重索引更新失败，请稍后重试"
                )
                return False
        self.pending_illust_ids: set[str] = set()
        self.sent_illust_ids: set[str] = set()
        self.unknown_illust_ids: set[str] = set()
        self.retained_paths: set[str] = set()
        self.file_transport = (
            self.event.get_platform_name() == AIOCQHTTP_PLATFORM
            and getattr(self.event, "bot", None) is not None
        )
        self.downloaded: list[tuple[dict, str, str, int]] = []
        self.temp_paths: list[str] = []
        self.image_sources: dict[str, str] = {}
        self.pending_suffix = (
            ":explicit_pending"
            if self.candidate_stream is not None
            and self.candidate_stream.query.mode == "artwork"
            else ""
        )
        self.attempted_ids: set[str] = set()
        self.batch_attempts = 1
        self.download_attempts = 0
        self.transient_offset = self.raw_count
        self.queue: list[dict] = []
        self.deadline = time.monotonic() + self.timeout_sec * max(3, self.count)
        if self.selection_deadline is not None:
            self.deadline = min(self.deadline, self.selection_deadline)
        self.failure_message = (
            "本次未找到符合筛选条件的可用图片，未放宽条件。"
            if self.selection.active
            else "😢 所有图片均下载失败，请稍后再试"
        )
        if self.tool and (not self.selection.active):
            self.failure_message = "本次未获取到可发送的图片。"
        return True

    async def _fetch_batch(self):
        """获取下一批候选，沿用当前入口的来源与游标。"""
        if self.candidate_stream is None:
            return await self.plugin._fetch_source_candidates(
                self.event, self.tag, count=SEARCH_BATCH_SIZE, policy=self.policy
            )
        before = self.candidate_stream.raw_count
        items = await self.candidate_stream.next_batch()
        return (
            items,
            self.candidate_stream.raw_count - before,
            self.candidate_stream.source_key,
        )

    async def _download_images(self):
        """有界补取和下载；每张失败时及时释放临时占用。"""
        self.queue = await self._pick_candidates(
            self.illusts, self.pick_count, raw_count=self.raw_count
        )
        if not self.queue and (not self.tool):
            self.result.message = (
                "当前去重范围内没有未发送过的图片了，换个标签或稍后再试"
            )
            return False
        while (
            len(self.downloaded) < self.count
            and self.download_attempts < SEARCH_DOWNLOAD_ATTEMPTS
        ):
            if time.monotonic() >= self.deadline:
                logger.info(
                    f"{LOG_PREFIX} 发图取图总时间预算耗尽: downloaded_count={len(self.downloaded)}"
                )
                break
            if not self.queue:
                remaining = [
                    item
                    for item in self.illusts
                    if str(item.get("id") or "") not in self.attempted_ids
                ]
                if remaining:
                    self.queue = await self._pick_candidates(
                        remaining,
                        min(self.count - len(self.downloaded), len(remaining)),
                    )
                if not self.queue:
                    if self.batch_attempts >= SEARCH_BATCH_ATTEMPTS or (
                        self.candidate_stream is not None
                        and self.candidate_stream.exhausted
                    ):
                        break
                    try:
                        if (
                            self.plugin.image_index is not None
                            and self.candidate_stream is None
                        ):
                            await self.plugin.image_index.advance_page_offset(
                                self.plugin._event_scope(self.event),
                                self.source_key,
                                self.raw_count,
                            )
                        fetch = (
                            self._fetch_batch()
                            if self.candidate_stream is not None
                            else self.plugin._fetch_source_candidates(
                                self.event,
                                self.tag,
                                count=SEARCH_BATCH_SIZE,
                                offset=self.transient_offset
                                if self.plugin.image_index is None
                                else 0,
                                policy=self.policy,
                            )
                        )
                        (
                            self.illusts,
                            self.raw_count,
                            self.source_key,
                        ) = await asyncio.wait_for(
                            fetch, timeout=max(0, self.deadline - time.monotonic())
                        )
                        if self.filter_manga:
                            self.illusts = self.plugin._filter_manga(self.illusts)
                        self.illusts = await self.plugin._filter_blacklisted_illusts(
                            self.illusts, self.policy
                        )
                        self.illusts = [
                            item
                            for item in self.illusts
                            if self.candidate_stream is not None
                            or self.selection.matches_dimensions(
                                item.get("width"), item.get("height")
                            )
                        ]
                    except Exception as exc:
                        logger.warning(
                            f"{LOG_PREFIX} 发图候选补取失败: error_type={type(exc).__name__}"
                        )
                        break
                    self.batch_attempts += 1
                    self.transient_offset += self.raw_count
                    continue
            illust = self.queue.pop(0)
            illust_id = illust.get("id", "?")
            item_id = str(illust.get("id") or "")
            if item_id in self.attempted_ids:
                continue
            self.attempted_ids.add(item_id)
            self.download_attempts += 1
            idx = self.download_attempts
            download_ready = False
            path = ""
            try:
                path, actual_q, file_size = await asyncio.wait_for(
                    self.plugin.downloader.download_for_send(
                        illust,
                        self.quality,
                        timeout=self.timeout_sec,
                        size_limit_bytes=self.size_limit_bytes,
                        log_context=f"[候选 {idx}/{SEARCH_DOWNLOAD_ATTEMPTS}] 作品 {illust_id}",
                        allow_quality_fallback=False,
                    ),
                    timeout=max(0, self.deadline - time.monotonic()),
                )
                self.temp_paths.append(path)
                if not self.selection.matches_file(path):
                    self._target_status(item_id, "filtered")
                    logger.debug(
                        f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} download_attempt={idx}/{SEARCH_DOWNLOAD_ATTEMPTS} reason=dimensions"
                    )
                    continue
                logger.debug(
                    f"{LOG_PREFIX} [候选 {idx}/{SEARCH_DOWNLOAD_ATTEMPTS}] 作品 {illust_id} 下载完成（大小={file_size / 1024:.2f}KB，画质={_search_quality_label(actual_q)}）"
                )
                self.downloaded.append((illust, path, actual_q, file_size))
                self.result.downloaded_count = len(self.downloaded)
                download_ready = True
            except asyncio.TimeoutError:
                self._target_status(item_id, "download_failed")
                logger.debug(
                    f"{LOG_PREFIX} 下载候选跳过: illust_id={illust_id} candidate={idx}/{SEARCH_DOWNLOAD_ATTEMPTS} reason=timeout timeout_seconds={self.timeout_sec}"
                )
            except Exception as e:
                self._target_status(item_id, "download_failed")
                logger.debug(
                    f"{LOG_PREFIX}"
                    f" 下载候选跳过: illust_id={illust_id}"
                    f" candidate={idx}"
                    f"/{SEARCH_DOWNLOAD_ATTEMPTS}"
                    f" reason=download_error error_type={type(e).__name__}"
                )
            finally:
                if not download_ready:
                    cleanup(path)
                item_id = str(illust.get("id") or "")
                if (
                    not download_ready
                    and self.plugin.image_index is not None
                    and (item_id in self.pending_illust_ids)
                ):
                    try:
                        await self.plugin.image_index.release_usage(
                            scope=self.plugin._event_scope(self.event),
                            source_key=self.image_sources[item_id]
                            + self.pending_suffix,
                            illust_id=item_id,
                            feature="normal_pending",
                        )
                        self.pending_illust_ids.discard(item_id)
                    except Exception as exc:
                        log_delivery_error(
                            exc,
                            context=f"illust_id={item_id}",
                            phase="release_usage",
                            delivery_status="not_submitted",
                        )
        return True

    async def _pick_candidates(self, items, amount, *, raw_count=0):
        """等待去重占用结果，再记录本次待清理状态。"""
        extra = (
            {
                "ordered": True,
                "allow_repeat": self.candidate_stream.query.mode == "artwork",
            }
            if self.candidate_stream is not None
            else {}
        )
        # 占用可能在线程内落库；取消后仍等待结果，再交给统一清理。
        pick_task = asyncio.create_task(
            self.plugin._pick_illusts(
                self.event,
                items,
                amount,
                source_key=self.source_key,
                dedupe_enabled=self.dedupe_days > 0,
                raw_count=raw_count,
                **extra,
            )
        )
        cancelled = False
        while True:
            try:
                picked = await asyncio.shield(pick_task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if pick_task.cancelled():
                    raise
        for item in picked:
            item_id = str(item.get("id") or "")
            self.image_sources[item_id] = self.source_key
            if self.plugin.image_index is not None and item_id:
                self.pending_illust_ids.add(item_id)
        if cancelled:
            raise asyncio.CancelledError
        return picked

    def _target_status(self, item_id, status):
        for target in self.result.targets:
            key = (
                target["id"]
                if target["page"] == 1
                else f"{target['id']}:{target['page'] - 1}"
            )
            if key == item_id:
                target["status"] = status

    async def _send_images(self):
        """按本次发送方式提交已下载图片。"""
        use_forward = self.plugin._should_use_forward(
            downloaded_count=len(self.downloaded),
            threshold=self.forward_threshold,
            platform_name=self.event.get_platform_name(),
        )
        if use_forward:
            raw_self_id = self.event.get_self_id()
            self_id = "0" if raw_self_id is None else str(raw_self_id)
            nodes = Nodes([])
            for illust, path, _actual_q, _file_size in self.downloaded:
                title = illust.get("title", "无标题")
                illust_id = illust.get("id", "?")
                content = [
                    Plain(f"🎨 {title} (ID: {illust_id})"),
                    Image.fromFileSystem(path),
                ]
                nodes.nodes.append(Node(uin=self_id, name="Pixiv", content=content))
            failed_count = self.count - len(self.downloaded)
            if failed_count > 0:
                nodes.nodes.append(
                    Node(
                        uin=self_id,
                        name="Pixiv",
                        content=[
                            Plain(
                                f"⚠️ 少 {failed_count} 张符合画质和筛选条件的可用图片，已跳过"
                            )
                        ],
                    )
                )
            await self._deliver_images(
                [nodes], self.downloaded, context="send_mode=forward"
            )
        else:
            for illust, path, actual_q, file_size in self.downloaded:
                illust_id = illust.get("id", "?")
                content = [
                    Plain(f"🎨 {illust.get('title', '无标题')} (ID: {illust_id})"),
                    Image.fromFileSystem(path),
                ]
                await self._deliver_images(
                    content,
                    [(illust, path, actual_q, file_size)],
                    context=f"send_mode=single illust_id={illust_id}",
                )

    async def _deliver_images(self, content, images, *, context: str):
        """按实际提交阶段维护计费、去重占用和文件读取窗口。"""
        ids = {str(illust["id"]) for illust, *_ in images if illust.get("id")}
        try:
            status = await send_once(self.event, content, context=context)
        except DeliveryNotSubmitted:
            raise
        except asyncio.CancelledError:
            # 提交期间取消也可能已送达，保留占用与协议端读取窗口。
            self._retain_unknown(ids, images)
            self.result.record_delivery(DeliveryStatus.UNKNOWN, len(images))
            raise
        self.result.record_delivery(status, len(images))
        for item_id in ids:
            self._target_status(item_id, status.value)
        if status is DeliveryStatus.UNKNOWN:
            self._retain_unknown(ids, images)
        if status in (
            DeliveryStatus.CONFIRMED,
            DeliveryStatus.ACCEPTED,
            DeliveryStatus.TIMED_OUT,
        ):
            if (
                status in (DeliveryStatus.ACCEPTED, DeliveryStatus.TIMED_OUT)
                and self.file_transport
            ):
                self.retained_paths.update((path for _, path, *_ in images))
            self.sent_illust_ids.update(ids)
            for illust, *_ in images:
                await self.plugin._record_image_usage(
                    self.event,
                    self.image_sources[str(illust.get("id") or "")],
                    illust,
                    feature="normal",
                    user_id=str(self.event.get_sender_id() or ""),
                )

    def _retain_unknown(self, ids, images):
        self.unknown_illust_ids.update(ids)
        for item_id in ids:
            self._target_status(item_id, "unknown")
        if self.file_transport:
            self.retained_paths.update((path for _, path, *_ in images))

    async def _finish(self):
        """等待结算清理完成，重复取消也不跳过收尾。"""
        # 结算与文件清理必须完成；重复取消不应跳过收尾。
        cleanup_task = asyncio.create_task(self._settle_and_cleanup())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup_task)
                break
            except asyncio.CancelledError:
                self.result.interrupted = True
                cancelled = True
                if cleanup_task.cancelled():
                    raise
        if cancelled:
            raise asyncio.CancelledError
        for target in self.result.targets:
            if target["status"] == "candidate":
                target["status"] = "not_submitted"

    async def _settle_and_cleanup(self):
        """按提交结果结算，保留送达未知的文件和去重占用。"""
        try:
            if self.charging and self.sent_illust_ids:
                total_cost = self.unit_cost * len(self.sent_illust_ids)
                self.result.settlement = "unknown"
                try:
                    result = await self.wallet.spend_coins(
                        user_id=self.sender_id, cost=total_cost
                    )
                    if not result.success:
                        self.result.settlement = "failed"
                        if self.tool:
                            self.result.message = "图片发送已尝试，但金币结算未完成，请联系管理员核对；不要重复取图。"
                        logger.warning(
                            f"{LOG_PREFIX} /p 金币结算未完成: sent_count={len(self.sent_illust_ids)} cost={total_cost} result_message={result.message}"
                        )
                        try:
                            if not self.tool:
                                await self.event.send(
                                    self.event.plain_result(
                                        f"⚠️ 金币结算未完成：{result.message}"
                                    )
                                )
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 发送金币结算警告失败: error_type={type(exc).__name__}"
                            )
                    else:
                        self.result.settlement = "completed"
                        after = result.profile.coins // self.unit_cost
                        before = (result.profile.coins + total_cost) // self.unit_cost
                        if (
                            self.tool
                            and (not self.result.notice)
                            and (before > 2)
                            and (after <= 2)
                        ):
                            self.result.notice = (
                                "剩下的金币快不够取图了，记得签到补充一下。"
                            )
                        logger.info(
                            f"{LOG_PREFIX} /p 金币结算: sent_count={len(self.sent_illust_ids)} cost={total_cost}"
                        )
                        try:
                            if not self.tool:
                                await self.event.send(
                                    self.event.plain_result(
                                        f"💰 本次发图消耗 {total_cost} 金币（{len(self.sent_illust_ids)} 张），余额 {result.profile.coins}。"
                                    )
                                )
                        except Exception as exc:
                            logger.warning(
                                f"{LOG_PREFIX} 发送金币结算提醒失败: error_type={type(exc).__name__}"
                            )
                except Exception as exc:
                    if self.tool:
                        self.result.message = (
                            "金币结算状态暂时无法确认，请联系管理员核对；不要重复取图。"
                        )
                    logger.warning(
                        f"{LOG_PREFIX} /p 金币结算调用异常，扣费状态待核对: error_type={type(exc).__name__}"
                    )
        finally:
            for p in self.temp_paths:
                if p in self.retained_paths:
                    self.plugin.downloader.defer_cleanup(p)
                else:
                    cleanup(p)
            if self.pending_illust_ids and self.plugin.image_index is not None:
                releasable = self.pending_illust_ids - self.unknown_illust_ids
                if not self.pending_suffix:
                    releasable -= self.sent_illust_ids
                for illust_id in releasable:
                    try:
                        await self.plugin.image_index.release_usage(
                            scope=self.plugin._event_scope(self.event),
                            source_key=self.image_sources[illust_id]
                            + self.pending_suffix,
                            illust_id=illust_id,
                            feature="normal_pending",
                        )
                    except Exception as exc:
                        log_delivery_error(
                            exc,
                            context=f"illust_id={illust_id}",
                            phase="release_usage",
                            delivery_status="not_confirmed",
                        )
