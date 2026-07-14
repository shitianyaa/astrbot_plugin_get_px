from __future__ import annotations

import asyncio
import os

from astrbot.api.all import Image, Plain, logger
from astrbot.api.event import AstrMessageEvent

from .downloader import cleanup, pick_image_url, pick_image_url_exact


LOG_PREFIX = "[GetPx]"
DEFAULT_AUTO_DOWNGRADE_ORIGINAL_LIMIT_MB = 3.0


class DeliveryMixin:
    """Direct illustration download and send-error handling."""

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
            logger.warning(
                f"{LOG_PREFIX} 下载前获取作品详情失败 illust_id={illust_id}: {e}"
            )
            yield event.plain_result("❌ 获取作品详情失败，请稍后再试")
            return

        if not illust:
            yield event.plain_result(f"😶 未找到作品 {illust_id}")
            return

        try:
            reason = await self._blacklist_reason_for_illust(illust, str(illust_id))
        except RuntimeError:
            yield event.plain_result("🚫 内容安全服务暂不可用，本次请求已拒绝")
            return
        if reason:
            yield event.plain_result(f"🚫 {reason}")
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
