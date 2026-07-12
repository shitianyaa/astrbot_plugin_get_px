from __future__ import annotations

import asyncio
from contextlib import closing
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

    def _export_snapshot_sync(self) -> dict[str, Any]:
        with closing(self._connect()) as conn:
            profiles = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_profiles ORDER BY user_id"
                ).fetchall()
            ]
            records = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM checkin_records ORDER BY date_key, user_id"
                ).fetchall()
            ]
            preferences = [dict(row) for row in conn.execute(
                "SELECT * FROM checkin_user_preferences ORDER BY user_id"
            ).fetchall()]
            global_events = [dict(row) for row in conn.execute(
                "SELECT * FROM checkin_global_events ORDER BY event_id"
            ).fetchall()]
            achievements = [dict(row) for row in conn.execute(
                "SELECT * FROM checkin_achievements ORDER BY user_id, achievement_id"
            ).fetchall()]
        return {
            "schema_version": CHECKIN_SNAPSHOT_SCHEMA_VERSION,
            "plugin_name": CHECKIN_SNAPSHOT_PLUGIN_NAME,
            "scope": CHECKIN_SNAPSHOT_SCOPE,
            "exported_at": self.now_iso(),
            "profiles": profiles,
            "records": records,
            "preferences": preferences,
            "global_events": global_events,
            "achievements": achievements,
        }
    def _import_snapshot_sync(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        normalized = _validate_checkin_snapshot(snapshot)
        profiles = normalized["profiles"]
        records = normalized["records"]
        preferences = normalized["preferences"]
        global_events = normalized["global_events"]
        achievements = normalized["achievements"]

        with closing(self._connect()) as conn:
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM checkin_records")
                conn.execute("DELETE FROM checkin_achievements")
                conn.execute("DELETE FROM checkin_global_events")
                conn.execute("DELETE FROM checkin_user_preferences")
                conn.execute("DELETE FROM checkin_profiles")
                if profiles:
                    conn.executemany(
                        """
                        INSERT INTO checkin_profiles (
                            user_id, coins, affection, total_days, streak_days,
                            last_checkin_date, boost_start_date, boost_until_date,
                            repeat_penalty_date, repeat_penalty_total,
                            created_at, updated_at
                        )
                        VALUES (
                            :user_id, :coins, :affection, :total_days, :streak_days,
                            :last_checkin_date, :boost_start_date, :boost_until_date,
                            :repeat_penalty_date, :repeat_penalty_total,
                            :created_at, :updated_at
                        )
                        """,
                        profiles,
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
                            secondary_note, template_version,
                            background_mode, background_source,
                            background_illust_id, background_title,
                            background_author, created_at, updated_at
                        )
                        VALUES (
                            :date_key, :user_id, :username, :bot_name,
                            :base_coins, :bonus_coins, :coins_reward,
                            :base_affection, :bonus_affection, :affection_reward,
                            :boost_active, :boost_multiplier,
                            :total_coins_after, :total_affection_after,
                            :total_days_after, :streak_days_after,
                            :note, :event_key, :event_label, :greeting,
                            :greeting_source, :greeting_attribution,
                            :secondary_note, :template_version,
                            :background_mode, :background_source,
                            :background_illust_id, :background_title,
                            :background_author, :created_at, :updated_at
                        )
                        """,
                        records,
                    )
                if preferences:
                    conn.executemany(
                        """
                        INSERT INTO checkin_user_preferences
                        (user_id, birthday_month, birthday_day, birthday_source,
                         qq_birthday_checked, selected_title_id, created_at, updated_at)
                        VALUES (:user_id, :birthday_month, :birthday_day, :birthday_source,
                                :qq_birthday_checked, :selected_title_id, :created_at, :updated_at)
                        """, preferences,
                    )
                if global_events:
                    conn.executemany(
                        """
                        INSERT INTO checkin_global_events
                        (event_id, event_type, date_value, name, created_by, created_at, updated_at)
                        VALUES (:event_id, :event_type, :date_value, :name, :created_by, :created_at, :updated_at)
                        """, global_events,
                    )
                if achievements:
                    conn.executemany(
                        """
                        INSERT INTO checkin_achievements (user_id, achievement_id, unlocked_at)
                        VALUES (:user_id, :achievement_id, :unlocked_at)
                        """, achievements,
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return {
            "schema_version": normalized["schema_version"],
            "plugin_name": normalized["plugin_name"],
            "scope": normalized["scope"],
            "profiles": len(profiles),
            "records": len(records),
            "preferences": len(preferences),
            "global_events": len(global_events),
            "achievements": len(achievements),
            "exported_at": normalized["exported_at"],
            "imported_at": self.now_iso(),
        }
