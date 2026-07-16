from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from astrbot.api import logger

LOG_PREFIX = "[GetPx]"

# Pixiv access_token 有效期约 3600 秒，提前 10 分钟刷新避免临界过期
TOKEN_REFRESH_INTERVAL_SECONDS = 3000.0


def _is_missing_illust_error(exc: Exception) -> bool:
    for attr in ("status", "status_code", "code"):
        value = getattr(exc, attr, None)
        if value == 404 or str(value) == "404":
            return True
    message = str(exc).casefold()
    return "404" in message or "not found" in message or "not_found" in message


@dataclass
class PixivClient:
    refresh_token: str
    proxy: str = ""
    request_timeout: float = 30.0
    close_timeout: float = 5.0

    _api: object = field(default=None, repr=False)
    _cached_token: str = field(default="", repr=False)
    _expires_at: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _idle: asyncio.Condition = field(init=False, repr=False)
    _active_calls: int = field(default=0, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self._idle = asyncio.Condition(self._lock)

    async def ensure_logged_in(self):
        async with self._lock:
            await self._ensure_logged_in_locked()

    async def _ensure_logged_in_locked(self) -> None:
        if self._closed:
            raise RuntimeError("Pixiv 客户端已关闭")
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
        try:
            await self._wait_for(
                api.login(refresh_token=self.refresh_token),
                operation="登录",
            )
        except BaseException:
            await self._close_api(api)
            raise

        self._api = api
        self._cached_token = self.refresh_token
        self._expires_at = now + TOKEN_REFRESH_INTERVAL_SECONDS
        logger.info(f"{LOG_PREFIX} Pixiv 登录成功")

    async def _acquire_api(self):
        async with self._lock:
            await self._ensure_logged_in_locked()
            self._active_calls += 1
            return self._api

    async def _release_api(self) -> None:
        async with self._lock:
            self._active_calls -= 1
            if self._active_calls == 0:
                self._idle.notify_all()

    @property
    def api(self):
        return self._api

    async def _wait_for(self, awaitable, *, operation: str):
        try:
            return await asyncio.wait_for(awaitable, timeout=self.request_timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError(f"Pixiv {operation}超时") from exc

    async def _wait_for_with_retry(self, factory, *, operation: str):
        """幂等请求失败时做一次轻量重试，缓解瞬时网络抖动。"""
        try:
            return await self._wait_for(factory(), operation=operation)
        except Exception as exc:
            if self._closed:
                raise
            logger.warning(f"{LOG_PREFIX} Pixiv {operation}失败，1 秒后重试: {exc}")
            await asyncio.sleep(1.0)
            if self._closed:
                raise RuntimeError("Pixiv 客户端已关闭") from exc
            return await self._wait_for(factory(), operation=operation)

    async def search(self, tag: str, offset: int = 0) -> list[dict]:
        api = await self._acquire_api()
        try:
            resp = await self._wait_for_with_retry(
                lambda: api.search_illust(
                    tag,
                    search_target="partial_match_for_tags",
                    sort="date_desc",
                    offset=offset,
                ),
                operation="搜索请求",
            )
        finally:
            await self._release_api()
        return list(resp.get("illusts") or [])

    async def recommended(self, offset: int = 0) -> list[dict]:
        api = await self._acquire_api()
        try:
            resp = await self._wait_for_with_retry(
                lambda: api.illust_recommended(offset=offset),
                operation="推荐作品请求",
            )
        finally:
            await self._release_api()
        return list(resp.get("illusts") or [])

    async def illust_detail(self, illust_id: int) -> dict | None:
        api = await self._acquire_api()
        try:
            resp = await self._wait_for(
                api.illust_detail(illust_id),
                operation="作品详情请求",
            )
            return resp.get("illust")
        except Exception as e:
            if _is_missing_illust_error(e):
                return None
            raise
        finally:
            await self._release_api()

    async def close(self):
        async with self._lock:
            self._closed = True
            try:
                await asyncio.wait_for(
                    self._wait_until_idle_locked(), timeout=self.close_timeout
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"{LOG_PREFIX} Pixiv 客户端关闭等待超时: "
                    f"active_calls={self._active_calls}"
                )
            api = self._api
            self._api = None
        await self._close_api(api)

    async def _wait_until_idle_locked(self) -> None:
        while self._active_calls:
            await self._idle.wait()

    async def _close_api(self, api) -> None:
        if api is None:
            return
        close = getattr(api, "close", None)
        if callable(close):
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await asyncio.wait_for(result, timeout=self.close_timeout)
            except Exception:
                pass
