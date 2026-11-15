"""图片指令的文字参数适配；作品流、发送与结算复用领域执行入口。"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.core.star.filter.command import GreedyStr

from .query import ArtworkQuery, QueryError
from .search_result import SearchResult

COMMAND_USAGE = {
    "search": "用法：/ps <关键词，可用空格组合> [数量]",
    "artwork": "用法：/pd <作品ID或标准Pixiv链接> [页码，默认1]",
    "ranking": "用法：/pr <日|周|月，也可用day|week|month> [数量]",
}
RANKING_NAMES = {
    "日": "day", "周": "week", "月": "month",
    "day": "day", "week": "week", "month": "month",
}


def split_tag_and_count(query: str) -> tuple[str, str]:
    """保留多词搜索文本；尾部独立数字作为数量，兼容原 /p。"""
    tokens = query.split()
    if not tokens:
        return "", ""
    if tokens[-1].isdigit():
        return " ".join(tokens[:-1]), tokens[-1]
    return " ".join(tokens), ""


def parse_command(mode: str, text: str) -> tuple[ArtworkQuery, int]:
    """将三个明确 Pixiv 指令适配为模型工具共用的查询对象。"""
    parts = text.split()
    args = {"mode": mode}
    count = 1
    try:
        if mode == "search":
            tag, amount = split_tag_and_count(text)
            args["tag"] = tag
            count = int(amount) if amount else 1
        elif mode == "artwork":
            if not 1 <= len(parts) <= 2:
                raise ValueError
            args["targets"] = [{
                "artwork": parts[0],
                "page": int(parts[1]) if len(parts) == 2 else 1,
            }]
        elif mode == "ranking":
            if not 1 <= len(parts) <= 2:
                raise ValueError
            args["ranking_mode"] = RANKING_NAMES[parts[0].lower()]
            count = int(parts[1]) if len(parts) == 2 else 1
        if count < 1:
            raise ValueError
    except (ValueError, KeyError):
        raise QueryError("invalid_request", COMMAND_USAGE[mode]) from None
    try:
        return ArtworkQuery.from_args(args), count
    except QueryError as exc:
        raise QueryError(exc.code, f"{exc} {COMMAND_USAGE[mode]}") from None


class PixivCommandMixin:
    """AstrBot 指令适配；不依赖 tools/ 的模型会话与重试策略。"""

    _split_tag_and_count = staticmethod(split_tag_and_count)

    async def _handle_pixiv_command(self, event, mode: str, raw_query):
        event.stop_event()
        text = "" if raw_query is GreedyStr else str(raw_query or "")
        try:
            query, count = parse_command(mode, text)
        except QueryError as exc:
            yield event.plain_result(str(exc))
            return
        if not self._cfg_str("pixiv_refresh_token"):
            yield event.plain_result("此指令需要配置 Pixiv Token，不回退其他图源。")
            return
        outcome = SearchResult(count)
        try:
            await self._execute_search(
                event, query.tag, str(count), query=query, result=outcome
            )
        except QueryError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:
            logger.warning(
                f"[GetPx] Pixiv 指令执行未完成: mode={mode} "
                f"error_type={type(exc).__name__}"
            )
            yield event.plain_result(
                "取图执行未完整结束，请核对实际收图和结算情况，勿重复补发。"
                if outcome.submission_started
                else "Pixiv 查询或准备失败，本次未提交图片。"
            )
            return
        if outcome.targets:
            reason = {
                "not_found": "未找到指定作品或当前账号无法访问。",
                "filtered": "指定作品未通过当前内容安全或类型筛选。",
                "page_unavailable": "指定作品没有该页，未改取其他页面。",
            }.get(outcome.targets[0]["status"])
            if reason:
                outcome.message = reason
        if outcome.message:
            yield event.plain_result(outcome.message)
