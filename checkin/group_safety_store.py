from __future__ import annotations

import asyncio
from contextlib import closing


class GroupSafetyStoreMixin:
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
            rows = conn.execute("SELECT group_id, general_only_enabled, builtin_terms_enabled, updated_by, updated_at FROM group_content_safety ORDER BY group_id").fetchall()
        return [{"group_id": str(row["group_id"]), "general_only_enabled": bool(row["general_only_enabled"]), "builtin_terms_enabled": bool(row["builtin_terms_enabled"]), "updated_by": str(row["updated_by"] or ""), "updated_at": str(row["updated_at"] or "")} for row in rows]
