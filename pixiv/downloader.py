from __future__ import annotations

import asyncio
import os
import re
import tempfile
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from PIL import Image, UnidentifiedImageError

from astrbot.api import logger

LOG_PREFIX = "[GetPx]"
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
MAX_PROXY_ORIGINS = 5
MAX_DOWNLOAD_ATTEMPTS = 8
_LOG_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)

ALLOWED_PIXIV_IMAGE_HOSTS = frozenset(
    {
        "i.pximg.net",
        "i.pixiv.re",
        "i.pixiv.cat",
        "proxy.pixivel.moe",
    }
)

QUALITY_ORDERS = {
    "original": ("original", "large", "medium", "square_medium"),
    "large": ("large", "medium", "square_medium", "original"),
    "medium": ("medium", "square_medium", "large", "original"),
    "square_medium": ("square_medium", "medium", "large", "original"),
}

QUALITY_LOG_LABELS = {
    "original": "原图",
    "large": "大图",
    "medium": "中图",
    "square_medium": "方形缩略图",
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


def _download_error_reason(exc: BaseException) -> str:
    """Return a stable reason code without exposing upstream response details."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, aiohttp.ClientResponseError):
        return f"http_{exc.status}" if exc.status else "http_error"
    if isinstance(exc, aiohttp.ClientError):
        return "network_error"
    if isinstance(exc, RuntimeError):
        message = str(exc)
        if message.startswith("HTTP "):
            status = message.removeprefix("HTTP ").strip()
            return f"http_{status}" if status.isdigit() else "http_error"
        if message.startswith("图片过大"):
            return "too_large"
        if message == "响应内容不是有效图片":
            return "invalid_image"
        if message == "原图超过自动降级阈值":
            return "downgrade_threshold"
        return "runtime_error"
    if isinstance(exc, OSError):
        return "filesystem_error"
    return "unexpected_error"


def _safe_log_context(value: object) -> str:
    compact = " ".join(str(value or "").split())
    return _LOG_URL_PATTERN.sub("[url]", compact)[:160]


def _download_route_log_label(route: str, source: object) -> str:
    if route == "proxy":
        return "反代地址"
    if str(source or "").casefold() == "lolicon":
        return "图片源返回地址"
    return "直连地址"


def parse_proxy_origins(raw_origins: object) -> tuple[str, ...]:
    """Parse user-configured HTTP(S) origins, preserving the first occurrence."""
    if isinstance(raw_origins, str):
        values: Iterable[object] = raw_origins.splitlines()
    elif isinstance(raw_origins, Iterable):
        values = raw_origins
    else:
        values = ()

    origins: list[str] = []
    seen: set[str] = set()
    invalid_lines: list[int] = []
    limit_warning_logged = False
    for index, raw_value in enumerate(values, 1):
        value = str(raw_value or "").strip()
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or "?" in value
                or "#" in value
                or "\\" in parsed.netloc
                or any(char.isspace() for char in parsed.netloc)
            ):
                raise ValueError("必须是仅含协议、主机和可选端口的 HTTP(S) origin")
            port = parsed.port
        except (ValueError, UnicodeError):
            invalid_lines.append(index)
            continue

        host = parsed.hostname.casefold()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        if (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)}:
            port = None
        netloc = f"{host}:{port}" if port is not None else host
        origin = urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))
        dedupe_key = origin.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        if len(origins) >= MAX_PROXY_ORIGINS:
            if not limit_warning_logged:
                logger.warning(
                    f"{LOG_PREFIX} Lolicon 图片反代地址超过上限，"
                    f"仅使用前 {MAX_PROXY_ORIGINS} 个有效地址"
                )
                limit_warning_logged = True
            break
        origins.append(origin)
    if invalid_lines:
        samples = ",".join(str(index) for index in invalid_lines[:3])
        logger.warning(
            f"{LOG_PREFIX} 已忽略无效 Lolicon 图片反代地址: "
            f"invalid_count={len(invalid_lines)} sample_lines={samples}"
        )
    return tuple(origins)


def rewrite_image_url_with_origin(url: str, origin: str) -> str:
    image = urlsplit(url)
    proxy = urlsplit(origin)
    return urlunsplit(
        (proxy.scheme, proxy.netloc, image.path, image.query, image.fragment)
    )


def iter_download_urls(
    original_url: str,
    *,
    source: object,
    proxy_origins: Iterable[str],
) -> Iterable[str]:
    if source == "lolicon":
        hostname = (urlsplit(original_url).hostname or "").casefold()
        if hostname in ALLOWED_PIXIV_IMAGE_HOSTS:
            for origin in proxy_origins:
                yield rewrite_image_url_with_origin(original_url, origin)
    yield original_url


class ImageDownloader:
    def __init__(self, lolicon_image_proxy_origins: object = ""):
        self._session: aiohttp.ClientSession | None = None
        self.lolicon_image_proxy_origins = parse_proxy_origins(
            lolicon_image_proxy_origins
        )
        logger.debug(
            f"{LOG_PREFIX} Lolicon 图片反代配置已加载: "
            f"valid_origins={len(self.lolicon_image_proxy_origins)} "
            f"attempt_budget={MAX_DOWNLOAD_ATTEMPTS}"
        )

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def download(self, url: str, timeout: float) -> str:
        session = self._ensure_session()
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        suffix = ".jpg"
        lower_path = url.split("?")[0].lower()
        for ext in (".png", ".gif", ".webp", ".jpeg"):
            if lower_path.endswith(ext):
                suffix = ext
                break

        fd = -1
        path = ""
        try:
            async with session.get(
                url,
                headers=PIXIV_HEADERS,
                timeout=client_timeout,
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                # 快速路径：服务器已声明体积且超限，直接拒绝
                content_length = resp.content_length
                if content_length and content_length > MAX_DOWNLOAD_BYTES:
                    raise _too_large_error(content_length)

                fd, path = tempfile.mkstemp(prefix="get_px_", suffix=suffix)
                size = 0
                file_obj = os.fdopen(fd, "wb")
                fd = -1
                with file_obj as f:
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise _too_large_error(size)
                        f.write(chunk)
            try:
                with Image.open(path) as image:
                    image.verify()
            except (OSError, UnidentifiedImageError) as exc:
                raise RuntimeError("响应内容不是有效图片") from exc
            return path
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError as exc:
                    logger.warning(
                        f"{LOG_PREFIX} 关闭图片临时文件描述符失败: "
                        f"error_type={type(exc).__name__}"
                    )
            cleanup(path)
            raise

    async def download_for_send(
        self,
        illust: dict,
        quality: str,
        timeout: float,
        downgrade_limit_bytes: int,
        log_context: str,
    ) -> tuple[str, str, int]:
        started_at = time.monotonic()
        log_context = _safe_log_context(log_context)
        tried_urls: set[str] = set()
        attempts = 0
        last_error: Exception | None = None
        last_reason = "no_candidate"
        failure_reasons: list[str] = []
        enforce_downgrade_limit = False
        candidates = list(iter_image_quality_urls(illust, quality))
        if not candidates:
            raise RuntimeError("无可下载 URL")

        def _plan_download_attempts(
            urls: list[str], tried: set[str], remaining_budget: int
        ) -> list[tuple[str, str]]:
            """规划下载尝试路由：返回 (route_type, url) 列表。"""
            returned_url = urls[-1]
            proxy_urls = [url for url in urls[:-1] if url != returned_url]
            returned_pending = returned_url not in tried
            reserved_for_returned = 1 if returned_pending and remaining_budget > 0 else 0
            selected_proxy_urls = proxy_urls[
                : max(0, remaining_budget - reserved_for_returned)
            ]
            routes = [("proxy", url) for url in selected_proxy_urls]
            if returned_pending and remaining_budget > 0:
                routes.append(("returned", returned_url))
            return routes

        for actual_quality, original_url in candidates:
            quality_error: Exception | None = None
            urls = list(
                iter_download_urls(
                    original_url,
                    source=illust.get("_source"),
                    proxy_origins=self.lolicon_image_proxy_origins,
                )
            )
            remaining_budget = MAX_DOWNLOAD_ATTEMPTS - attempts
            download_routes = _plan_download_attempts(urls, tried_urls, remaining_budget)
            has_proxy_routes = any(route == "proxy" for route, _ in download_routes)
            for route, url in download_routes:
                if attempts >= MAX_DOWNLOAD_ATTEMPTS:
                    break
                if url in tried_urls:
                    continue
                tried_urls.add(url)
                attempts += 1
                if route == "returned" and has_proxy_routes:
                    logger.debug(
                        f"{LOG_PREFIX} {log_context} 反代候选不可用，"
                        f"尝试 Lolicon 返回地址: quality={actual_quality} "
                        f"attempt={attempts}/{MAX_DOWNLOAD_ATTEMPTS}"
                    )
                path = ""
                try:
                    path = await self.download(url, timeout=timeout)
                    file_size = os.path.getsize(path)
                except Exception as exc:
                    cleanup(path)
                    quality_error = exc
                    last_error = exc
                    last_reason = _download_error_reason(exc)
                    failure_reasons.append(last_reason)
                    logger.debug(
                        f"{LOG_PREFIX} {log_context} 图片下载候选失败: "
                        f"quality={actual_quality} route={route} "
                        f"attempt={attempts}/{MAX_DOWNLOAD_ATTEMPTS} "
                        f"reason={last_reason} error_type={type(exc).__name__}"
                    )
                    continue

                within_downgrade_limit = (
                    downgrade_limit_bytes <= 0
                    or file_size <= downgrade_limit_bytes
                )
                if (
                    actual_quality != "original"
                    and not enforce_downgrade_limit
                ) or within_downgrade_limit:
                    quality_label = QUALITY_LOG_LABELS.get(
                        actual_quality, actual_quality
                    )
                    route_label = _download_route_log_label(
                        route, illust.get("_source")
                    )
                    logger.info(
                        f"{LOG_PREFIX} {log_context} 图片下载完成："
                        f"画质={quality_label} 下载路径={route_label} "
                        f"尝试次数={attempts} "
                        f"大小={file_size / 1024:.2f}KB "
                        f"耗时={int((time.monotonic() - started_at) * 1000)}ms"
                    )
                    return path, actual_quality, file_size

                if actual_quality == "original":
                    enforce_downgrade_limit = True
                    logger.info(
                        f"{LOG_PREFIX} {log_context} 原图超过 "
                        f"{downgrade_limit_bytes / 1024 / 1024:.2f} MiB "
                        f"({file_size / 1024 / 1024:.2f} MiB)，自动降低质量"
                    )
                else:
                    logger.debug(
                        f"{LOG_PREFIX} {log_context} 降档图片仍超过阈值: "
                        f"quality={actual_quality} size_bytes={file_size}"
                    )
                cleanup(path)
                quality_error = RuntimeError("原图超过自动降级阈值")
                last_error = quality_error
                last_reason = _download_error_reason(quality_error)
                failure_reasons.append(last_reason)
                break

            if attempts >= MAX_DOWNLOAD_ATTEMPTS:
                logger.warning(
                    f"{LOG_PREFIX} {log_context} 图片下载尝试预算已耗尽: "
                    f"attempts={attempts}/{MAX_DOWNLOAD_ATTEMPTS} "
                    f"quality={actual_quality} last_reason={last_reason}"
                )
                break
            if quality_error is not None:
                logger.debug(
                    f"{LOG_PREFIX} {log_context} quality={actual_quality} 不可用，"
                    "尝试下一质量"
                )

        if last_error is not None:
            logger.warning(
                f"{LOG_PREFIX} {log_context} 图片下载最终失败: "
                f"attempts={attempts} last_reason={last_reason}"
            )
            message = (
                f"图片下载失败，已尝试 {attempts} 个地址 "
                f"(reason={last_reason})"
            )
            if failure_reasons and all(
                reason == "timeout" for reason in failure_reasons
            ):
                raise asyncio.TimeoutError(message) from last_error
            raise RuntimeError(message) from last_error
        raise RuntimeError("无可下载 URL")


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
    return next((url for _, url in iter_image_quality_urls(illust, quality)), "")


def iter_image_quality_urls(
    illust: dict, quality: str = "original"
) -> Iterable[tuple[str, str]]:
    order = QUALITY_ORDERS.get(quality, QUALITY_ORDERS["original"])
    seen_urls: set[str] = set()
    for candidate_quality in order:
        url = pick_image_url_exact(illust, candidate_quality)
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        yield candidate_quality, url


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
    except OSError as exc:
        logger.warning(
            f"{LOG_PREFIX} 清理图片临时文件失败: "
            f"error_type={type(exc).__name__}"
        )
