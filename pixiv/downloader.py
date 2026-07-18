from __future__ import annotations

import os
import tempfile

import aiohttp
from aiohttp_socks import ProxyConnector

from astrbot.api import logger

from .proxy import (
    is_pixiv_image_url,
    is_socks_proxy,
    normalize_proxy_url,
    rewrite_pixiv_image_url,
)

LOG_PREFIX = "[GetPx]"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
CONTENT_TYPE_SUFFIXES = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

PIXIV_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.pixiv.net/",
}


def _too_large_error(size_bytes: float) -> RuntimeError:
    return RuntimeError(
        f"图片过大: {size_bytes / 1024 / 1024:.1f} MiB，"
        f"超过上限 {MAX_DOWNLOAD_BYTES / 1024 / 1024:.0f} MiB"
    )


class ImageDownloader:
    def __init__(self):
        self._sessions: dict[str, aiohttp.ClientSession] = {}

    def _ensure_session(
        self, proxy: str
    ) -> tuple[aiohttp.ClientSession, str | None]:
        normalized_proxy = normalize_proxy_url(proxy)
        session_key = normalized_proxy if is_socks_proxy(normalized_proxy) else "http"
        session = self._sessions.get(session_key)
        if session is None or session.closed:
            if is_socks_proxy(normalized_proxy):
                connector = ProxyConnector.from_url(normalized_proxy, rdns=True)
                session = aiohttp.ClientSession(connector=connector)
            else:
                session = aiohttp.ClientSession()
            self._sessions[session_key] = session
        request_proxy = None if is_socks_proxy(normalized_proxy) else normalized_proxy or None
        return session, request_proxy

    async def close(self):
        sessions, self._sessions = list(self._sessions.values()), {}
        for session in sessions:
            if not session.closed:
                await session.close()

    async def download(
        self,
        url: str,
        proxy: str,
        timeout: float,
        reverse_proxy_host: str = "",
    ) -> str:
        use_reverse_proxy = bool(reverse_proxy_host) and is_pixiv_image_url(url)
        url = rewrite_pixiv_image_url(url, reverse_proxy_host)
        session, request_proxy = self._ensure_session("" if use_reverse_proxy else proxy)
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        suffix = ".jpg"
        lower_path = url.split("?")[0].lower()
        for ext in (".png", ".gif", ".webp", ".jpeg"):
            if lower_path.endswith(ext):
                suffix = ext
                break

        path = ""
        try:
            request_kwargs = {
                "headers": PIXIV_HEADERS,
                "timeout": client_timeout,
            }
            if request_proxy:
                request_kwargs["proxy"] = request_proxy
            async with session.get(url, **request_kwargs) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                response_content_type = str(
                    getattr(resp, "content_type", "") or ""
                ).casefold()
                suffix = CONTENT_TYPE_SUFFIXES.get(response_content_type, suffix)
                # 快速路径：服务器已声明体积且超限，直接拒绝
                content_length = resp.content_length
                if content_length and content_length > MAX_DOWNLOAD_BYTES:
                    raise _too_large_error(content_length)

                fd, path = tempfile.mkstemp(prefix="get_px_", suffix=suffix)
                size = 0
                with os.fdopen(fd, "wb") as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise _too_large_error(size)
                        f.write(chunk)
            return path
        except Exception:
            cleanup(path)
            raise

    async def download_for_send(
        self,
        illust: dict,
        quality: str,
        proxy: str,
        timeout: float,
        downgrade_limit_bytes: int,
        log_context: str,
        reverse_proxy_host: str = "",
    ) -> tuple[str, str, int]:
        tried_urls: set[str] = set()

        url = pick_image_url(illust, quality)
        if not url:
            raise RuntimeError("无可下载 URL")
        tried_urls.add(url)

        actual_quality = _quality_from_url(url)
        logger.info(f"{LOG_PREFIX} {log_context} 下载 quality={actual_quality}")
        path = await self.download(
            url,
            proxy=proxy,
            timeout=timeout,
            reverse_proxy_host=reverse_proxy_host,
        )
        file_size = os.path.getsize(path)

        if (
            downgrade_limit_bytes <= 0
            or actual_quality != "original"
            or file_size <= downgrade_limit_bytes
        ):
            return path, actual_quality, file_size

        logger.info(
            f"{LOG_PREFIX} {log_context} 原图超过 {downgrade_limit_bytes / 1024 / 1024:.2f} MiB "
            f"({file_size / 1024 / 1024:.2f} MiB)，自动降低质量"
        )

        downgrade_succeeded = False
        for candidate_quality in ("large", "medium", "square_medium"):
            url = pick_image_url_exact(illust, candidate_quality)
            if not url or url in tried_urls:
                continue
            tried_urls.add(url)

            logger.info(f"{LOG_PREFIX} {log_context} 下载 quality={candidate_quality}")
            try:
                candidate_path = await self.download(
                    url,
                    proxy=proxy,
                    timeout=timeout,
                    reverse_proxy_host=reverse_proxy_host,
                )
                candidate_size = os.path.getsize(candidate_path)
            except Exception as e:
                logger.warning(
                    f"{LOG_PREFIX} {log_context} 降级到 {candidate_quality} 失败: {e}"
                )
                continue

            cleanup(path)
            path = candidate_path
            actual_quality = candidate_quality
            file_size = candidate_size
            downgrade_succeeded = True

            if file_size <= downgrade_limit_bytes:
                break

            logger.info(
                f"{LOG_PREFIX} {log_context} 降级后仍超过 {downgrade_limit_bytes / 1024 / 1024:.2f} MiB "
                f"({file_size / 1024 / 1024:.2f} MiB)，继续尝试更低质量"
            )

        if not downgrade_succeeded:
            cleanup(path)
            raise RuntimeError(
                f"原图超过 {downgrade_limit_bytes / 1024 / 1024:.2f} MiB 且降级图片不可用"
            )

        return path, actual_quality, file_size


def _quality_from_url(url: str) -> str:
    return (
        "original"
        if "original" in url
        else "large"
        if "large" in url
        else "square_medium"
        if "square_medium" in url
        else "medium"
        if "medium" in url
        else "square_medium"
    )


def pick_image_url(illust: dict, quality: str = "original") -> str:
    quality_order = {
        "original": ["original", "large", "medium", "square_medium"],
        "large": ["large", "medium", "square_medium", "original"],
        "medium": ["medium", "square_medium", "large", "original"],
    }
    order = quality_order.get(quality, quality_order["original"])

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

    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        first_urls = (meta_pages[0] or {}).get("image_urls") or {}
        for q in order:
            if first_urls.get(q):
                return first_urls[q]

    return ""


def pick_image_url_exact(illust: dict, quality: str) -> str:
    meta_single = illust.get("meta_single_page") or {}
    image_urls = illust.get("image_urls") or {}

    if quality == "original":
        url = meta_single.get("original_image_url") or image_urls.get("original")
        if url:
            return url
    else:
        url = image_urls.get(quality)
        if url:
            return url

    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        first_urls = (meta_pages[0] or {}).get("image_urls") or {}
        return first_urls.get(quality, "")

    return ""


def cleanup(path: str):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
