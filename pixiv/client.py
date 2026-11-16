from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from importlib.metadata import version
from urllib.parse import parse_qs, urlsplit

from astrbot.api import logger

from .query import ArtworkQuery, QueryError
from .network import normalize_proxy_url
from .pagination import parse_continuation, validate_continuation


@dataclass
class PixivPage:
    items: list[dict]
    next_params: dict | None = None


@dataclass
class SourceRequest:
    """普通抽图的一次请求选定来源后，沿真实游标续查；Lolicon仅有限重抽。"""

    source: str = ""
    next_params: dict | None = None
    exhausted: bool = False
    retried_first_page: bool = False


def _probe_next_page(value, endpoint):
    """只记录续页形状，不输出地址、参数值或响应原文。"""
    summary = {"next_type": type(value).__name__, "has_next_url": bool(value)}
    if not isinstance(value, str) or not value:
        return json.dumps(summary, ensure_ascii=True)
    try:
        parsed, expected = urlsplit(value), urlsplit(endpoint)
        summary["host_matches"] = parsed.netloc == expected.netloc
        summary["path_matches"] = parsed.path == expected.path
        params = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4096)
        known = {
            "offset", "viewed", "bookmark_illust_ids", "seed_illust_ids",
            "max_bookmark_id_for_recommend", "min_bookmark_id_for_recent_illust",
            "content_type", "filter", "include_ranking_label", "include_ranking_illusts",
            "include_privacy_policy", "illust_id", "user_id", "word", "sort", "search_target",
        }
        fields = {}
        unknown = 0
        for key, values in params.items():
            name = re.sub(r"\[[0-9]*\]$", "", key)
            if name not in known:
                unknown += 1
                continue
            shape = "empty" if any(not value for value in values) else "digits" if all(value.isascii() and value.isdigit() for value in values) else "text"
            entry = fields.setdefault(name, {"count": 0, "shapes": []})
            entry["count"] += len(values)
            if shape not in entry["shapes"]:
                entry["shapes"].append(shape)
        summary.update(fields=fields, unknown_fields=unknown)
    except ValueError:
        summary["parse_failed"] = True
    return json.dumps(summary, ensure_ascii=True, sort_keys=True)

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
    request_timeout: float = 30.0
    close_timeout: float = 5.0
    proxy_url: str = field(default="", repr=False)

    _api: object = field(default=None, repr=False)
    _cached_token: str = field(default="", repr=False)
    _expires_at: float = field(default=0.0, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _idle: asyncio.Condition = field(init=False, repr=False)
    _active_calls: int = field(default=0, repr=False)
    _closed: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        self.proxy_url = normalize_proxy_url(self.proxy_url)
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

        api = AppPixivAPI(proxy=self.proxy_url or None, env=False)
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
            logger.warning(
                f"{LOG_PREFIX} Pixiv {operation}失败，1 秒后重试: "
                f"error_type={type(exc).__name__}"
            )
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

    async def query_page(self, query: ArtworkQuery, continuation: dict | None = None) -> PixivPage:
        routes = {
            "search": ("search_illust", {"word": query.tag, "search_target": query.search_target,
                       "sort": query.sort, "start_date": query.start_date, "end_date": query.end_date}),
            "recommended": ("illust_recommended", {}),
            "ranking": ("illust_ranking", {"mode": query.ranking_mode, "date": query.date}),
            "author": ("user_illusts", {"user_id": query.user_id, "type": "illust"}),
            "related": ("illust_related", {"illust_id": query.illust_id}),
        }
        name, params = routes[query.mode]
        return await self._public_page(name, params, continuation)

    async def users_page(self, word: str, continuation: dict | None = None) -> PixivPage:
        return await self._public_page("search_user", {"word": word}, continuation, field="user_previews")

    async def _public_page(self, name, params, continuation, *, field="illusts") -> PixivPage:
        if continuation is not None:
            try:
                continuation = validate_continuation(name, continuation)
            except ValueError as exc:
                raise QueryError("invalid_request", "分页参数与接口不匹配。") from exc
        api = await self._acquire_api()
        probe = (
            f"probe_id={time.monotonic_ns()} source=pixiv endpoint={name} "
            f"sdk_version={version('pixivpy-async')} "
            f"network_route={urlsplit(self.proxy_url).scheme or 'direct'}"
        )
        phase = "request"
        started = time.monotonic()
        logger.info(f"{LOG_PREFIX} 查询探针请求: {probe} "
                    f"cursor_keys={','.join(sorted(continuation or {})) or 'none'}")
        try:
            method, endpoint = getattr(api.api, "illust_recommended_auth" if name == "illust_recommended" else name)
            if name == "illust_related" and continuation and "viewed" in continuation:
                # SDK 的关联方法未暴露 viewed；仍只请求固定关联接口。
                query_params = dict(params, filter="for_ios")
                for key, value in continuation.items():
                    if isinstance(value, list):
                        query_params.update({f"{key}[{index}]": item for index, item in enumerate(value)})
                    else:
                        query_params[key] = value
                request = api.requests_(method=method, url=endpoint, params=query_params)
            else:
                request = getattr(api, name)(**params, **(continuation or {}))
            response = await self._wait_for(
                request, operation="公开查询"
            )
            phase = "response_validation"
            items = response.get(field) if isinstance(response, dict) else None
            error = response.get("error") if isinstance(response, dict) else None
            error_text = str(error).casefold() if error else ""
            error_kind = (
                "auth_hint" if any(word in error_text for word in ("oauth", "invalid_grant", "authentication", "access token")) else
                "rate_limit_hint" if any(word in error_text for word in ("rate limit", "rate_limit", "too many", "429")) else
                "other" if error else "none"
            )
            logger.info(
                f"{LOG_PREFIX} 查询探针响应: {probe} response_type={type(response).__name__} "
                f"items_type={type(items).__name__} candidate_count={len(items) if isinstance(items, list) else 0} "
                f"upstream_error={error_kind} "
                f"cursor={_probe_next_page(response.get('next_url') if isinstance(response, dict) else None, endpoint)}"
            )
            if not isinstance(response, dict) or response.get("error") or not isinstance(response.get(field), list):
                raise QueryError("upstream_error", "Pixiv 查询失败或响应结构无效。")
            if any(not isinstance(item, dict) for item in response[field]):
                raise QueryError("upstream_error", "Pixiv 候选结构无效。")
            phase = "pagination"
            page = PixivPage(response[field], parse_continuation(response.get("next_url"), endpoint, name))
            logger.info(
                f"{LOG_PREFIX} 查询探针完成: {probe} candidate_count={len(page.items)} "
                f"has_next={'yes' if page.next_params is not None else 'no'} "
                f"duration_ms={int((time.monotonic() - started) * 1000)}"
            )
            return page
        except Exception as exc:
            code = exc.code if isinstance(exc, QueryError) and exc.code in {"invalid_request", "upstream_error"} else "none"
            status = getattr(exc, "status", None)
            logger.warning(
                f"{LOG_PREFIX} 查询探针失败: {probe} phase={phase} error_type={type(exc).__name__} "
                f"error_code={code} http_status={status if type(status) is int and 100 <= status <= 599 else 'unknown'} "
                f"duration_ms={int((time.monotonic() - started) * 1000)}"
            )
            raise
        finally:
            await self._release_api()

    async def public_detail(self, illust_id: int) -> dict | None:
        """新查询入口不把认证或上游错误当作作品不存在。"""
        api = await self._acquire_api()
        try:
            response = await self._wait_for(api.illust_detail(illust_id), operation="作品公开查询")
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, dict) and str(error.get("message", "")).strip().casefold().replace("_", " ") == "illust not found":
                return None
            if not isinstance(response, dict) or response.get("error") or "illust" not in response:
                raise QueryError("upstream_error", "Pixiv 作品查询失败。")
            item = response.get("illust")
            if item is not None and (not isinstance(item, dict) or str(item.get("id")) != str(illust_id)):
                raise QueryError("upstream_error", "Pixiv 作品详情结构无效。")
            return item
        except Exception as exc:
            if _is_missing_illust_error(exc):
                return None
            raise
        finally:
            await self._release_api()

    async def public_user(self, user_id: int) -> dict:
        api = await self._acquire_api()
        try:
            response = await self._wait_for(api.user_detail(user_id), operation="作者公开查询")
            if (not isinstance(response, dict) or response.get("error")
                    or not isinstance(response.get("user"), dict)
                    or str(response["user"].get("id")) != str(user_id)):
                raise QueryError("upstream_error", "Pixiv 作者查询失败或响应结构无效。")
            return response["user"]
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
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 关闭 Pixiv API 会话失败: "
                    f"error_type={type(exc).__name__}"
                )
