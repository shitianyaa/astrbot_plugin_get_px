"""按需读取公开作品热度；不携带账号凭据或下载图片。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json

import aiohttp
from astrbot.api import logger

from .query import positive_id

STATS_TIMEOUT = 3.0
MAX_STATS_BYTES = 256 * 1024
STAT_FIELDS = {
    "likeCount": "total_likes",
    "bookmarkCount": "total_bookmarks",
    "viewCount": "total_view",
}


@dataclass
class ArtworkStatsClient:
    _session: aiohttp.ClientSession | None = field(default=None, repr=False)
    _closed: bool = field(default=False, repr=False)
    _semaphore: asyncio.Semaphore = field(
        default_factory=lambda: asyncio.Semaphore(3), repr=False
    )

    def _ensure_session(self) -> aiohttp.ClientSession:
        if self._closed:
            raise RuntimeError("作品热度客户端已关闭")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.pixiv.net/"},
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return self._session

    async def fetch(self, illust_id: str) -> dict:
        identity = positive_id(illust_id)
        session = self._ensure_session()
        async with session.get(
            f"https://www.pixiv.net/ajax/illust/{identity}",
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=STATS_TIMEOUT),
        ) as response:
            if response.status != 200:
                raise ValueError("作品热度请求失败")
            data = bytearray()
            async for chunk in response.content.iter_chunked(16 * 1024):
                data.extend(chunk)
                if len(data) > MAX_STATS_BYTES:
                    raise ValueError("作品热度响应超限")
            payload = json.loads(data)
        body = payload.get("body") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("error") is not False
            or not isinstance(body, dict)
            or str(body.get("illustId")) != str(identity)
        ):
            raise ValueError("作品热度响应无效")
        return {
            target: body[source]
            for source, target in STAT_FIELDS.items()
            if type(body.get(source)) is int and body[source] >= 0
        }

    async def enrich(self, records: list[dict]) -> None:
        by_id: dict[str, list[dict]] = {}
        for record in records:
            by_id.setdefault(record["id"], []).append(record)

        async def update(identity, targets):
            async with self._semaphore:
                try:
                    stats = await self.fetch(identity)
                except Exception as exc:
                    logger.warning(
                        f"[GetPx] 作品热度补查失败: error_type={type(exc).__name__}"
                    )
                    return
                for target in targets:
                    target.update(stats)

        await asyncio.gather(*(update(key, value) for key, value in by_id.items()))

    async def close(self) -> None:
        self._closed = True
        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()
