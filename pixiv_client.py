from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from astrbot.api import logger

LOG_PREFIX = "[GetPx]"


@dataclass
class PixivClient:
    refresh_token: str
    proxy: str = ""

    _api: object = field(default=None, repr=False)
    _cached_token: str = field(default="", repr=False)
    _expires_at: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    async def ensure_logged_in(self):
        async with self._lock:
            now = time.monotonic()
            if (
                self._api is not None
                and self._cached_token == self.refresh_token
                and now < self._expires_at
            ):
                return

            try:
                from pixivpy_async import AppPixivAPI
            except ImportError:
                raise RuntimeError(
                    "未安装 pixivpy-async，请运行: pip install pixivpy-async"
                )

            api = AppPixivAPI(proxy=self.proxy) if self.proxy else AppPixivAPI()
            await api.login(refresh_token=self.refresh_token)

            self._api = api
            self._cached_token = self.refresh_token
            self._expires_at = now + 3000
            logger.info(f"{LOG_PREFIX} Pixiv 登录成功")

    @property
    def api(self):
        return self._api

    async def search(self, tag: str) -> list[dict]:
        await self.ensure_logged_in()
        resp = await self._api.search_illust(
            tag, search_target="partial_match_for_tags", sort="date_desc"
        )
        return list(resp.get("illusts") or [])

    async def ranking(self, mode: str = "week") -> list[dict]:
        await self.ensure_logged_in()
        resp = await self._api.illust_ranking(mode=mode)
        return list(resp.get("illusts") or [])

    async def illust_detail(self, illust_id: int) -> dict | None:
        await self.ensure_logged_in()
        try:
            resp = await self._api.illust_detail(illust_id)
            return resp.get("illust")
        except Exception:
            return None

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
