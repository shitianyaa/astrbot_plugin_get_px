from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

from .models import (
    CHECKIN_SNAPSHOT_PLUGIN_NAME,
    CHECKIN_SNAPSHOT_SCHEMA_VERSION,
    CHECKIN_SNAPSHOT_SCOPE,
)
from .snapshot import validate_checkin_snapshot as _validate_checkin_snapshot


class BackupStoreMixin:
    async def export_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._export_snapshot_sync)

    async def import_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            return await asyncio.to_thread(self._import_snapshot_sync, snapshot)

    async def import_snapshot_with_rollback(
        self,
        snapshot: dict[str, Any],
        write_rollback: Callable[[dict[str, Any]], Path],
    ) -> tuple[Path, dict[str, Any]]:
        """在同一把锁内导出回滚备份并导入快照。

        回滚备份和导入必须原子完成，否则两步之间的并发签到会被导入
        覆盖、却不在回滚备份里。
        """
        async with self._lock:
            rollback_snapshot = await asyncio.to_thread(self._export_snapshot_sync)
            rollback_path = await asyncio.to_thread(write_rollback, rollback_snapshot)
            result = await asyncio.to_thread(self._import_snapshot_sync, snapshot)
        return rollback_path, result

    def _export_snapshot_sync(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            users = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_users ORDER BY user_id"
                ).fetchall()
            ]
            records = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_records ORDER BY date_key, user_id"
                ).fetchall()
            ]
            global_events = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_global_events ORDER BY event_id"
                ).fetchall()
            ]
            achievements = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_achievements ORDER BY user_id, achievement_id"
                ).fetchall()
            ]
            user_themes = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM checkin_user_themes
                    ORDER BY user_id, acquired_at, theme_id
                    """
                ).fetchall()
            ]
            group_presence = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM checkin_group_presence
                    ORDER BY date_key, group_id, user_id
                    """
                ).fetchall()
            ]
        return {
            "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
            "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
            "scope": CHECKIN_SNAPSHOT_SCOPE,
            "exported_at": self.now_iso(),
            "users": users,
            "records": records,
            "global_events": global_events,
            "achievements": achievements,
            "user_themes": user_themes,
            "group_presence": group_presence,
        }

    def _import_snapshot_sync(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        normalized = _validate_checkin_snapshot(snapshot)
        users = normalized["users"]
        records = normalized["records"]
        global_events = normalized["global_events"]
        achievements = normalized["achievements"]
        user_themes = normalized["user_themes"]
        group_presence = normalized["group_presence"]

        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM checkin_group_presence")
                conn.execute("DELETE FROM checkin_records")
                conn.execute("DELETE FROM checkin_achievements")
                conn.execute("DELETE FROM checkin_user_themes")
                conn.execute("DELETE FROM checkin_global_events")
                conn.execute("DELETE FROM checkin_users")
                if users:
                    conn.executemany(
                        """
                        INSERT INTO checkin_users (
                            user_id, coins, affection, total_days, streak_days,
                            last_checkin_date, boost_start_date, boost_until_date,
                            repeat_penalty_date, repeat_penalty_total,
                            birthday_month, birthday_day, birthday_source,
                            qq_birthday_checked, selected_title_id, current_theme_id,
                            created_at, updated_at
                        ) VALUES (
                            :user_id, :coins, :affection, :total_days, :streak_days,
                            :last_checkin_date, :boost_start_date, :boost_until_date,
                            :repeat_penalty_date, :repeat_penalty_total,
                            :birthday_month, :birthday_day, :birthday_source,
                            :qq_birthday_checked, :selected_title_id, :current_theme_id,
                            :created_at, :updated_at
                        )
                        """,
                        users,
                    )
                if user_themes:
                    conn.executemany(
                        """
                        INSERT INTO checkin_user_themes
                            (user_id, theme_id, price_paid, acquired_at)
                        VALUES (:user_id, :theme_id, :price_paid, :acquired_at)
                        """,
                        user_themes,
                    )
                if records:
                    conn.executemany(
                        """
                        INSERT INTO checkin_records (
                            date_key, user_id, username, bot_name,
                            base_coins, bonus_coins, coins_reward,
                            base_affection, bonus_affection, affection_reward,
                            boost_active, boost_multiplier,
                            total_coins_after, total_affection_after,
                            total_days_after, streak_days_after,
                            note, event_key, event_label, greeting,
                            greeting_source, greeting_attribution,
                            secondary_note, template_version, theme_id, render_tier,
                            background_mode, background_source,
                            background_illust_id, background_title,
                            background_author, background_quality,
                            created_at, updated_at
                        ) VALUES (
                            :date_key, :user_id, :username, :bot_name,
                            :base_coins, :bonus_coins, :coins_reward,
                            :base_affection, :bonus_affection, :affection_reward,
                            :boost_active, :boost_multiplier,
                            :total_coins_after, :total_affection_after,
                            :total_days_after, :streak_days_after,
                            :note, :event_key, :event_label, :greeting,
                            :greeting_source, :greeting_attribution,
                            :secondary_note, :template_version, :theme_id, :render_tier,
                            :background_mode, :background_source,
                            :background_illust_id, :background_title,
                            :background_author, :background_quality,
                            :created_at, :updated_at
                        )
                        """,
                        records,
                    )
                if global_events:
                    conn.executemany(
                        """
                        INSERT INTO checkin_global_events
                            (event_id, event_type, date_value, name, created_by,
                             created_at, updated_at)
                        VALUES (:event_id, :event_type, :date_value, :name,
                                :created_by, :created_at, :updated_at)
                        """,
                        global_events,
                    )
                if achievements:
                    conn.executemany(
                        """
                        INSERT INTO checkin_achievements
                            (user_id, achievement_id, unlocked_at)
                        VALUES (:user_id, :achievement_id, :unlocked_at)
                        """,
                        achievements,
                    )
                if group_presence:
                    conn.executemany(
                        """
                        INSERT INTO checkin_group_presence (
                            date_key, group_id, group_name, platform, user_id,
                            username, first_seen_at, last_seen_at
                        ) VALUES (
                            :date_key, :group_id, :group_name, :platform, :user_id,
                            :username, :first_seen_at, :last_seen_at
                        )
                        """,
                        group_presence,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "schema_version": normalized["schema_version"],
            "plugin_name": normalized["plugin_name"],
            "scope": normalized["scope"],
            "users": len(users),
            "records": len(records),
            "global_events": len(global_events),
            "achievements": len(achievements),
            "user_themes": len(user_themes),
            "group_presence": len(group_presence),
            "exported_at": normalized["exported_at"],
            "imported_at": self.now_iso(),
        }
