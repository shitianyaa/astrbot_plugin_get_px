"""公开查询的请求复用、有界内存续页与独立限频。"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass, field, replace
import hashlib
import json
import secrets
import time

from astrbot.api import logger

from .catalog import CandidateStream, artwork_record, user_record
from .query import ArtworkQuery, QUERY_KEYS, QueryError, positive_id
from .selection import ImageSelection, SELECTION_PARAMETERS
from .stats import ArtworkStatsClient, STATS_TIMEOUT

CURSOR_TTL = 600.0
CURSOR_LIMIT = 128


@dataclass
class UserStream:
    plugin: object
    event: object
    word: str
    policy: object
    continuation: dict | None = None
    exhausted: bool = False
    raw_count: int = 0
    filtered_count: int = 0
    sampled: bool = False

    async def next_batch(self):
        client = CandidateStream(
            self.plugin, self.event, ArtworkQuery(), self.policy
        ).client()
        page = await client.users_page(self.word, self.continuation)
        if page.next_params is not None and page.next_params == self.continuation:
            raise QueryError("upstream_error", "作者查询重复返回相同续页位置。")
        self.continuation, self.exhausted = page.next_params, page.next_params is None
        self.raw_count += len(page.items)
        users = []
        for preview in page.items:
            user = preview.get("user") or {}
            if user.get("id") and not await self.plugin._blocked_query_term(
                f"{user.get('name', '')} {user.get('account', '')}", self.policy
            ):
                users.append(user)
        self.filtered_count += len(page.items) - len(users)
        return users


@dataclass
class CursorState:
    binding: str
    kind: str
    stream: object
    limit: int
    buffer: list[dict] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    expires: float = 0.0
    result: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    include_stats: bool = False


class DiscoveryService:
    EVENT_KEY = "get_px_public_queries"
    RATE_KEY = "get_px_public_query_admitted"

    def __init__(self, plugin):
        self.plugin = plugin
        self.cursors: OrderedDict[str, CursorState] = OrderedDict()
        self.last_request: dict[str, float] = {}
        self.stats_client = ArtworkStatsClient()

    def clear(self):
        self.cursors.clear()
        self.last_request.clear()

    async def binding(self, event, policy):
        terms = await self.plugin._safety_terms(policy)
        blocked = await self.plugin._blacklisted_illust_ids(policy)
        identity = (
            event.unified_msg_origin,
            event.get_sender_id(),
            policy.cache_identity(),
            sorted(terms),
            sorted(blocked),
            self.plugin._cfg_bool("filter_manga", True),
            self.plugin._cfg_str("pixiv_refresh_token", ""),
            self.plugin._cfg_str("preferred_image_source", "Lolicon"),
            self.plugin._cfg_bool("lolicon_exclude_ai", True),
        )
        return hashlib.sha256(repr(identity).encode()).hexdigest()

    def _store(self, state):
        now = time.monotonic()
        for token in tuple(self.cursors):
            if self.cursors[token].expires <= now:
                del self.cursors[token]
        token = secrets.token_urlsafe(24)
        state.expires = now + CURSOR_TTL
        self.cursors[token] = state
        while len(self.cursors) > CURSOR_LIMIT:
            self.cursors.popitem(last=False)
        return token

    def _admit(self, event):
        if event.get_extra(self.RATE_KEY):
            return
        now = time.monotonic()
        interval = self.plugin._cfg_int("rate_limit_seconds", 3, 0, 60)
        sender = event.get_sender_id()
        elapsed = now - self.last_request.get(sender, float("-inf"))
        if interval > elapsed:
            raise QueryError(
                "rate_limited",
                f"公开查询还需等待 {int(interval - elapsed) + 1} 秒，本次未开始查询。",
            )
        if len(self.last_request) >= 1024:
            self.last_request = {
                key: value
                for key, value in self.last_request.items()
                if now - value < 60
            }
        self.last_request[sender] = now
        event.set_extra(self.RATE_KEY, True)

    async def query(self, event, kind, args, timeout):
        try:
            policy = await self.plugin._content_safety_policy(event)
            binding = await self.binding(event, policy)
            if "cursor" in args:
                self._cursor(kind, args, binding)
            key = json.dumps([kind, binding, args], sort_keys=True, ensure_ascii=False)
            memo = event.get_extra(self.EVENT_KEY)
            if memo is None:
                memo = {}
                event.set_extra(self.EVENT_KEY, memo)
            if key in memo:
                return deepcopy(await asyncio.shield(memo[key]))
            self._admit(event)
            future = asyncio.get_running_loop().create_future()
            memo[key] = future
            try:
                budget = min(
                    timeout, self.plugin._cfg_float("request_timeout", 30.0, 5.0, 120.0)
                )
                deadline = time.monotonic() + budget
                result = await asyncio.wait_for(
                    self._execute(event, kind, args, policy, binding), budget
                )
                if result.get("stats_requested"):
                    await self._enrich_stats(result["records"], deadline)
            except asyncio.CancelledError:
                future.cancel()
                raise
            except Exception as exc:
                result = self.error(exc)
            future.set_result(deepcopy(result))
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self.error(exc)

    async def _enrich_stats(self, records, deadline):
        for record in records:
            for key in ("total_likes", "total_bookmarks", "total_view"):
                value = record.get(key)
                if type(value) is not int or value < 0:
                    record[key] = None
        remaining = min(STATS_TIMEOUT, deadline - time.monotonic())
        if not records or remaining <= 0:
            return
        try:
            await asyncio.wait_for(self.stats_client.enrich(records), remaining)
        except asyncio.TimeoutError:
            logger.info("[GetPx] 作品热度补查预算耗尽: stats_status=partial")
        except Exception as exc:
            logger.warning(
                f"[GetPx] 作品热度补查未完成: error_type={type(exc).__name__}"
            )

    @staticmethod
    def error(exc):
        if isinstance(exc, QueryError):
            code, message = exc.code, str(exc)
        elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            code, message = (
                "budget_exhausted",
                "公开查询等待预算已用尽，本次没有发送或扣费。",
            )
        else:
            logger.warning(f"[GetPx] 公开查询失败: error_type={type(exc).__name__}")
            code, message = (
                "query_unavailable",
                "公开查询服务或图片源暂不可用，本次没有发送或扣费。",
            )
        return {
            "status": code,
            "records": [],
            "message": message,
            "retry_allowed": False,
        }

    async def _execute(self, event, kind, args, policy, binding):
        if "cursor" in args:
            token = args["cursor"]
            state = self._cursor(kind, args, binding)
            self.cursors.move_to_end(token)
            async with state.lock:
                if state.result is None:
                    state.result = await self._page(state)
                return deepcopy(state.result)
        limit = args.get("limit", 5)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise QueryError("invalid_request", "查询数量需为1至20。")
        include_stats = args.get("include_stats", False)
        if kind == "artworks":
            if type(include_stats) is not bool:
                raise QueryError("invalid_request", "include_stats 必须为布尔值。")
            if (
                set(args)
                - QUERY_KEYS
                - (SELECTION_PARAMETERS - {"max_size_mib"})
                - {"limit", "include_stats"}
            ):
                raise QueryError("invalid_request", "作品查询参数不受支持。")
            stream = CandidateStream(
                self.plugin,
                event,
                ArtworkQuery.from_args(args),
                policy,
                ImageSelection.from_args(args),
            )
        else:
            if set(args) - {"word", "user_id", "limit"} or ("word" in args) == (
                "user_id" in args
            ):
                raise QueryError(
                    "invalid_request",
                    "作者查询需要 word 或 user_id，二者不能同时指定。",
                )
            if "user_id" in args:
                user_id = positive_id(args["user_id"])
                client = CandidateStream(
                    self.plugin, event, ArtworkQuery(), policy
                ).client()
                user = await client.public_user(user_id)
                blocked = await self.plugin._blocked_query_term(
                    f"{user.get('name', '')} {user.get('account', '')}", policy
                )
                return {
                    "status": "filtered" if blocked else "ok",
                    "records": [] if blocked else [user_record(user)],
                    "pagination": {"returned": 0 if blocked else 1, "has_more": False},
                    "retry_allowed": False,
                }
            word = args["word"]
            if (
                not isinstance(word, str)
                or not word.strip()
                or len(word) > 48
                or any(ord(c) < 32 for c in word)
            ):
                raise QueryError(
                    "invalid_request", "作者关键词需为不含控制字符的短文本。"
                )
            if await self.plugin._blocked_query_term(word, policy):
                raise QueryError("filtered", "作者关键词不符合当前内容安全要求。")
            stream = UserStream(self.plugin, event, word.strip(), policy)
        return await self._page(CursorState(binding, kind, stream, limit, include_stats=include_stats))

    def _cursor(self, kind, args, binding):
        if set(args) != {"cursor"} or not isinstance(args["cursor"], str):
            raise QueryError("invalid_request", "续查只接受 cursor，不可修改查询参数。")
        state = self.cursors.get(args["cursor"])
        if (
            state is None
            or state.expires <= time.monotonic()
            or state.binding != binding
            or state.kind != kind
        ):
            raise QueryError(
                "cursor_invalid", "续查游标已失效或不属于当前查询会话，未重新开始查询。"
            )
        return state

    async def _page(self, state):
        # 先复制输入状态；超时或取消不消耗已发给调用方的游标。
        stream = replace(state.stream)
        if isinstance(stream, CandidateStream):
            stream.target_results = list(stream.target_results)
        buffer, seen, records = list(state.buffer), set(state.seen), []
        batches = 0
        while len(records) < state.limit:
            if not buffer:
                if stream.exhausted or batches >= 3:
                    break
                seen.clear()
                buffer.extend(await stream.next_batch())
                batches += 1
                if not buffer:
                    continue
            item = buffer.pop(0)
            identity = str(item.get("id") or "")
            if not identity or identity in seen:
                continue
            seen.add(identity)
            records.append(
                user_record(item) if state.kind == "users" else artwork_record(item)
            )
        more = bool(buffer) or not stream.exhausted
        pagination = {"returned": len(records), "has_more": more}
        if more:
            pagination["next_cursor"] = self._store(
                CursorState(
                    state.binding, state.kind, stream, state.limit, buffer, seen,
                    include_stats=state.include_stats,
                )
            )
        status = (
            "ok"
            if records
            else "filtered"
            if stream.filtered_count > state.stream.filtered_count
            else "empty"
        )
        result = {
            "status": status,
            "records": records,
            "pagination": pagination,
            "source": "lolicon" if stream.sampled else "pixiv",
            "retry_allowed": False,
            "guidance": "这里只返回元数据，没有发送图片、扣费或识别画面；候选不代表已送达，不推断图库总量。",
        }
        if state.include_stats:
            result["stats_requested"] = True
        if stream.sampled:
            result["sampling"] = "random"
            result["guidance"] += "这是随机取样，没有真实下一页，再查可能重复。"
        if isinstance(stream, CandidateStream) and stream.target_results:
            result["targets"] = stream.target_results
        if not records:
            result["message"] = (
                "本批没有通过过滤的候选。"
                if status == "filtered"
                else "图片源未返回候选。"
            )
        return result
