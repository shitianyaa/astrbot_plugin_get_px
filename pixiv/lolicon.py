from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiohttp

from astrbot.api import logger


LOG_PREFIX = "[GetPx]"
DEFAULT_API_URL = "https://api.lolicon.app/setu/v2"
IMAGE_SIZES = ("original", "regular", "small", "thumb", "mini")


@dataclass
class LoliconClient:
    """Small async client for the Lolicon setu API."""

    api_url: str = DEFAULT_API_URL
    exclude_ai: bool = True
    request_timeout: float = 30.0
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def available(self) -> bool:
        return bool(self.api_url.strip()) and not self._closed

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("Lolicon 客户端已关闭")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def fetch(
        self,
        *,
        tag: str = "",
        count: int = 20,
        aspect_ratio: str = "",
    ) -> list[dict[str, Any]]:
        if not self.available:
            raise RuntimeError("Lolicon API 未配置")
        params: list[tuple[str, str]] = [
            ("r18", "0"),
            ("num", str(max(1, min(int(count), 20)))),
            ("excludeAI", "true" if self.exclude_ai else "false"),
        ]
        params.extend(("size", size) for size in IMAGE_SIZES)
        if tag.strip():
            params.append(("tag", tag.strip()))
        if aspect_ratio:
            params.append(("aspectRatio", aspect_ratio))

        session = self._ensure_session()
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        async with session.get(self.api_url, params=params, timeout=timeout) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Lolicon API HTTP {resp.status}")
            payload = await resp.json(content_type=None)
        if not isinstance(payload, dict):
            raise RuntimeError("Lolicon API 返回格式无效")
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        return [self._normalize(item) for item in payload.get("data") or []]

    async def search(
        self, tag: str, *, count: int = 20, aspect_ratio: str = ""
    ) -> list[dict[str, Any]]:
        return await self.fetch(tag=tag, count=count, aspect_ratio=aspect_ratio)

    async def random(
        self, *, count: int = 20, aspect_ratio: str = ""
    ) -> list[dict[str, Any]]:
        return await self.fetch(count=count, aspect_ratio=aspect_ratio)

    @staticmethod
    def _normalize(item: dict[str, Any]) -> dict[str, Any]:
        urls = item.get("urls") or {}
        pid = str(item.get("pid") or "")
        page = int(item.get("p") or 0)
        page_id = f"{pid}:{page}" if pid else ""
        tags = item.get("tags") or []
        return {
            "id": page_id,
            "pid": pid,
            "page": page,
            "title": str(item.get("title") or "无标题"),
            "user": {
                "id": str(item.get("uid") or ""),
                "name": str(item.get("author") or ""),
            },
            "x_restrict": 1 if item.get("r18") else 0,
            "width": item.get("width") or 0,
            "height": item.get("height") or 0,
            "tags": [{"name": str(tag)} for tag in tags],
            "ai_type": item.get("aiType", 0),
            "type": "illust",
            "meta_single_page": {
                "original_image_url": str(urls.get("original") or ""),
            },
            "image_urls": {
                "large": str(urls.get("regular") or ""),
                "medium": str(urls.get("small") or ""),
                "square_medium": str(urls.get("thumb") or urls.get("mini") or ""),
            },
            "_source": "lolicon",
        }

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None and not session.closed:
            try:
                await session.close()
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} Lolicon 客户端关闭失败: "
                    f"error_type={type(exc).__name__}"
                )
