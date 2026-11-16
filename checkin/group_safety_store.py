from __future__ import annotations

import asyncio
from contextlib import closing


class GroupSafetyStoreMixin:
    """旧表只读归档桥接，不提供策略查询或写入。"""

    async def list_group_content_safety_records(self) -> list[dict[str, object]]:
        async with self._lock:
            return await asyncio.to_thread(self._list_group_content_safety_records_sync)

    def _list_group_content_safety_records_sync(self) -> list[dict[str, object]]:
        with closing(self._connect()) as conn:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = 'group_content_safety'"
            ).fetchone():
                return []
            rows = conn.execute(
                "SELECT * FROM group_content_safety ORDER BY group_id"
            ).fetchall()
        return [dict(row) for row in rows]
