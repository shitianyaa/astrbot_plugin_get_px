from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date, timedelta
import re


_MONTH_PATTERN = re.compile(r"\d{4}-\d{2}")
RANKING_TYPES = {"today", "month", "streak", "total"}


class RankingStoreMixin:
    async def list_checkin_groups(self) -> list[dict[str, object]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_checkin_groups_sync)

    async def get_group_ranking(
        self,
        *,
        group_id: str,
        ranking_type: str = "today",
        month: str = "",
        limit: int = 10,
    ) -> dict[str, object]:
        group_id = str(group_id or "").strip()
        if not group_id:
            raise ValueError("group_id is required")
        ranking_type = str(ranking_type or "today").strip().lower()
        if ranking_type not in RANKING_TYPES:
            raise ValueError("ranking type must be today, month, streak or total")
        month = str(month or self.today_key()[:7]).strip()
        if not _MONTH_PATTERN.fullmatch(month):
            raise ValueError("month must use YYYY-MM")
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise ValueError("month must use YYYY-MM") from exc
        limit = max(1, min(int(limit), 100))
        async with self._lock:
            return await asyncio.to_thread(
                self._get_group_ranking_sync,
                group_id,
                ranking_type,
                month,
                limit,
            )

    async def get_group_trend(
        self, *, group_id: str, days: int = 7
    ) -> list[dict[str, object]]:
        group_id = str(group_id or "").strip()
        if not group_id:
            raise ValueError("group_id is required")
        days = 30 if int(days) == 30 else 7
        async with self._lock:
            return await asyncio.to_thread(self._get_group_trend_sync, group_id, days)

    async def get_checkin_overview(self) -> dict[str, int]:
        async with self._lock:
            return await asyncio.to_thread(self._get_checkin_overview_sync)

    def _list_checkin_groups_sync(self) -> list[dict[str, object]]:
        today = self.today_key()
        month = today[:7]
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT group_id,
                       COALESCE(NULLIF(MAX(group_name), ''), group_id) AS group_name,
                       MAX(platform) AS platform,
                       MAX(last_seen_at) AS last_seen_at,
                       COUNT(DISTINCT CASE WHEN date_key = ? THEN user_id END) AS today_count,
                       COUNT(DISTINCT CASE WHEN substr(date_key, 1, 7) = ?
                                      THEN date_key || ':' || user_id END) AS month_count
                FROM checkin_group_presence
                GROUP BY group_id
                ORDER BY last_seen_at DESC, group_id
                """,
                (today, month),
            ).fetchall()
        return [dict(row) for row in rows]

    def _get_group_ranking_sync(
        self, group_id: str, ranking_type: str, month: str, limit: int
    ) -> dict[str, object]:
        today = self.today_key()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT date_key, user_id, username, first_seen_at, last_seen_at
                FROM checkin_group_presence
                WHERE group_id = ?
                ORDER BY date_key, first_seen_at, user_id
                """,
                (group_id,),
            ).fetchall()
        grouped: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault(str(item["user_id"]), []).append(item)

        entries: list[dict[str, object]] = []
        for user_id, user_rows in grouped.items():
            latest = max(user_rows, key=lambda item: str(item["last_seen_at"]))
            dates = sorted({str(item["date_key"]) for item in user_rows})
            first_seen = min(str(item["first_seen_at"]) for item in user_rows)
            if ranking_type == "today":
                matching = [item for item in user_rows if item["date_key"] == today]
                if not matching:
                    continue
                first_seen = min(str(item["first_seen_at"]) for item in matching)
                value: object = first_seen
            elif ranking_type == "month":
                matching_dates = [
                    item for item in dates if item.startswith(month + "-")
                ]
                if not matching_dates:
                    continue
                value = len(matching_dates)
                month_rows = [
                    item
                    for item in user_rows
                    if str(item["date_key"]).startswith(month + "-")
                ]
                first_seen = min(str(item["first_seen_at"]) for item in month_rows)
            elif ranking_type == "streak":
                value = _current_streak(dates, today)
                if not value:
                    continue
            else:
                value = len(dates)
            entries.append(
                {
                    "user_id": user_id,
                    "username": str(latest["username"] or user_id),
                    "value": value,
                    "first_seen_at": first_seen,
                }
            )

        if ranking_type == "today":
            entries.sort(key=lambda item: (item["value"], item["user_id"]))
        else:
            entries.sort(
                key=lambda item: (
                    -int(item["value"]),
                    item["first_seen_at"],
                    item["user_id"],
                )
            )
        for rank, entry in enumerate(entries, 1):
            entry["rank"] = rank
        return {
            "group_id": group_id,
            "type": ranking_type,
            "month": month,
            "total": len(entries),
            "entries": entries[:limit],
            "all_entries": entries,
        }

    def _get_group_trend_sync(
        self, group_id: str, days: int
    ) -> list[dict[str, object]]:
        end = date.fromisoformat(self.today_key())
        start = end - timedelta(days=days - 1)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT date_key, COUNT(DISTINCT user_id) AS count
                FROM checkin_group_presence
                WHERE group_id = ? AND date_key BETWEEN ? AND ?
                GROUP BY date_key
                """,
                (group_id, start.isoformat(), end.isoformat()),
            ).fetchall()
        counts = {str(row["date_key"]): int(row["count"]) for row in rows}
        return [
            {
                "date": (start + timedelta(days=index)).isoformat(),
                "count": counts.get((start + timedelta(days=index)).isoformat(), 0),
            }
            for index in range(days)
        ]

    def _get_checkin_overview_sync(self) -> dict[str, int]:
        today = self.today_key()
        month = today[:7]
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT COUNT(DISTINCT CASE WHEN date_key = ? THEN user_id END) AS today_checkins,
                       COUNT(DISTINCT CASE WHEN date_key = ? THEN group_id END) AS active_groups,
                       COUNT(DISTINCT CASE WHEN substr(date_key, 1, 7) = ? THEN user_id END) AS month_users
                FROM checkin_group_presence
                """,
                (today, today, month),
            ).fetchone()
        return {
            "today_checkins": int(row["today_checkins"] or 0),
            "active_groups": int(row["active_groups"] or 0),
            "month_users": int(row["month_users"] or 0),
        }


def _current_streak(date_keys: list[str], today_key: str) -> int:
    if not date_keys:
        return 0
    today = date.fromisoformat(today_key)
    signed = {date.fromisoformat(item) for item in date_keys}
    cursor = today if today in signed else today - timedelta(days=1)
    if cursor not in signed:
        return 0
    streak = 0
    while cursor in signed:
        streak += 1
        cursor -= timedelta(days=1)
    return streak
