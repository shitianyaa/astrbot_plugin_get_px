from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date
import sqlite3

from .models import (
    ACHIEVEMENTS,
    CheckinGlobalEvent,
    CheckinProfile,
    CheckinUserPreference,
    ThemePurchaseResult,
)
from .snapshot import (
    clean_event_name as _clean_event_name,
    validate_global_event_date as _validate_global_event_date,
    validate_month_day as _validate_month_day,
)


class FeatureStoreMixin:
    async def find_user_preference(self, user_id: str) -> CheckinUserPreference | None:
        user_id = str(user_id or "")
        if not user_id:
            return None
        async with self._lock:
            return await asyncio.to_thread(self._find_preference_sync, user_id)

    async def get_user_preference(self, user_id: str) -> CheckinUserPreference:
        async with self._lock:
            return await asyncio.to_thread(
                self._get_or_create_preference_sync, str(user_id or "")
            )

    async def set_birthday(
        self, *, user_id: str, month: int, day: int, source: str
    ) -> CheckinUserPreference:
        _validate_month_day(month, day)
        if source not in {"manual", "qq"}:
            raise ValueError("birthday source must be manual or qq")
        async with self._lock:
            return await asyncio.to_thread(
                self._set_birthday_sync, str(user_id or ""), month, day, source
            )

    async def set_qq_birthday_if_not_manual(
        self, *, user_id: str, month: int, day: int
    ) -> CheckinUserPreference:
        _validate_month_day(month, day)
        async with self._lock:
            return await asyncio.to_thread(
                self._set_qq_birthday_if_not_manual_sync,
                str(user_id or ""),
                month,
                day,
            )

    async def clear_birthday(self, user_id: str) -> CheckinUserPreference:
        async with self._lock:
            return await asyncio.to_thread(
                self._clear_birthday_sync, str(user_id or "")
            )

    async def mark_qq_birthday_checked(self, user_id: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._mark_qq_birthday_checked_sync, str(user_id or "")
            )

    async def unlock_achievements(self, profile: CheckinProfile) -> tuple[str, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._unlock_achievements_sync, profile)

    async def list_achievements(self, user_id: str) -> tuple[str, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_achievements_sync, str(user_id or "")
            )

    async def select_title(self, *, user_id: str, title: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(
                self._select_title_sync, str(user_id or ""), str(title or "")
            )

    async def list_owned_theme_ids(self, user_id: str) -> tuple[str, ...]:
        async with self._lock:
            return await asyncio.to_thread(
                self._list_owned_theme_ids_sync, str(user_id or "")
            )

    async def purchase_theme(
        self,
        *,
        user_id: str,
        theme_id: str,
        cost: int | None = None,
    ) -> ThemePurchaseResult:
        if cost is not None and (
            isinstance(cost, bool) or not isinstance(cost, int) or not 0 <= cost <= 5000
        ):
            raise ValueError("theme cost must be an integer between 0 and 5000")
        async with self._lock:
            return await asyncio.to_thread(
                self._purchase_theme_sync,
                str(user_id or ""),
                str(theme_id or ""),
                cost,
            )

    async def select_theme(self, *, user_id: str, theme_id: str) -> str:
        async with self._lock:
            return await asyncio.to_thread(
                self._select_theme_sync,
                str(user_id or ""),
                str(theme_id or ""),
            )

    async def add_global_event(
        self, *, event_type: str, date_value: str, name: str, created_by: str
    ) -> CheckinGlobalEvent:
        normalized_type, normalized_date = _validate_global_event_date(
            event_type, date_value
        )
        clean_name = _clean_event_name(name)
        async with self._lock:
            return await asyncio.to_thread(
                self._add_global_event_sync,
                normalized_type,
                normalized_date,
                clean_name,
                str(created_by or ""),
            )

    async def list_global_events(self) -> tuple[CheckinGlobalEvent, ...]:
        async with self._lock:
            return await asyncio.to_thread(self._list_global_events_sync)

    async def delete_global_event(self, event_id: int) -> bool:
        async with self._lock:
            return await asyncio.to_thread(
                self._delete_global_event_sync, int(event_id)
            )

    async def events_for_date(self, date_key: str) -> tuple[CheckinGlobalEvent, ...]:
        day = date.fromisoformat(date_key)
        async with self._lock:
            return await asyncio.to_thread(
                self._events_for_date_sync, day.isoformat(), day.strftime("%m-%d")
            )

    def _get_or_create_preference_sync(self, user_id: str) -> CheckinUserPreference:
        if not user_id:
            raise ValueError("user_id is required")
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO checkin_users
                (user_id, created_at, updated_at) VALUES (?, ?, ?)
                """,
                (user_id, now, now),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO checkin_user_themes
                    (user_id, theme_id, price_paid, acquired_at)
                VALUES (?, 'default', 0, ?)
                """,
                (user_id, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM checkin_users WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._row_to_preference(row)

    def _find_preference_sync(self, user_id: str) -> CheckinUserPreference | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM checkin_users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return self._row_to_preference(row) if row is not None else None

    def _set_birthday_sync(
        self, user_id: str, month: int, day: int, source: str
    ) -> CheckinUserPreference:
        self._get_or_create_preference_sync(user_id)
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_users
                SET birthday_month = ?, birthday_day = ?, birthday_source = ?,
                    qq_birthday_checked = 1, updated_at = ? WHERE user_id = ?
                """,
                (month, day, source, now, user_id),
            )
            conn.commit()
        return self._get_or_create_preference_sync(user_id)

    def _set_qq_birthday_if_not_manual_sync(
        self, user_id: str, month: int, day: int
    ) -> CheckinUserPreference:
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE checkin_users
                SET birthday_month = ?, birthday_day = ?, birthday_source = 'qq',
                    qq_birthday_checked = 1, updated_at = ?
                WHERE user_id = ? AND birthday_source != 'manual'
                """,
                (month, day, self.now_iso(), user_id),
            )
            conn.commit()
        return self._get_or_create_preference_sync(user_id)

    def _clear_birthday_sync(self, user_id: str) -> CheckinUserPreference:
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_users SET birthday_month = 0,
                    birthday_day = 0, birthday_source = '', qq_birthday_checked = 0,
                    updated_at = ? WHERE user_id = ?
                """,
                (self.now_iso(), user_id),
            )
            conn.commit()
        return self._get_or_create_preference_sync(user_id)

    def _mark_qq_birthday_checked_sync(self, user_id: str) -> None:
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE checkin_users SET qq_birthday_checked = 1, updated_at = ? WHERE user_id = ?",
                (self.now_iso(), user_id),
            )
            conn.commit()

    def _unlock_achievements_sync(self, profile: CheckinProfile) -> tuple[str, ...]:
        now = self.now_iso()
        unlocked: list[str] = []
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for achievement_id, definition in ACHIEVEMENTS.items():
                value = (
                    profile.total_days
                    if definition["kind"] == "total"
                    else profile.streak_days
                )
                if value < int(definition["threshold"]):
                    continue
                changed = conn.execute(
                    "INSERT OR IGNORE INTO checkin_achievements (user_id, achievement_id, unlocked_at) VALUES (?, ?, ?)",
                    (profile.user_id, achievement_id, now),
                ).rowcount
                if changed:
                    unlocked.append(achievement_id)
            preference = conn.execute(
                "SELECT selected_title_id FROM checkin_users WHERE user_id = ?",
                (profile.user_id,),
            ).fetchone()
            if preference is None:
                conn.execute(
                    "INSERT INTO checkin_users (user_id, created_at, updated_at) VALUES (?, ?, ?)",
                    (profile.user_id, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO checkin_user_themes
                        (user_id, theme_id, price_paid, acquired_at)
                    VALUES (?, 'default', 0, ?)
                    """,
                    (profile.user_id, now),
                )
                selected = ""
            else:
                selected = str(preference["selected_title_id"] or "")
            unlocked_rows = conn.execute(
                "SELECT achievement_id FROM checkin_achievements WHERE user_id = ?",
                (profile.user_id,),
            ).fetchall()
            unlocked_ids = {str(row["achievement_id"]) for row in unlocked_rows}
            auto_title_id = next(
                (
                    achievement_id
                    for achievement_id in reversed(tuple(ACHIEVEMENTS))
                    if achievement_id in unlocked_ids
                ),
                "",
            )
            if not selected and auto_title_id:
                conn.execute(
                    "UPDATE checkin_users SET selected_title_id = ?, updated_at = ? WHERE user_id = ?",
                    (auto_title_id, now, profile.user_id),
                )
            conn.commit()
        return tuple(unlocked)

    def _list_achievements_sync(self, user_id: str) -> tuple[str, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT achievement_id FROM checkin_achievements WHERE user_id = ? ORDER BY unlocked_at, achievement_id",
                (user_id,),
            ).fetchall()
        return tuple(str(row["achievement_id"]) for row in rows)

    def _select_title_sync(self, user_id: str, title: str) -> str:
        title_id = (
            title
            if title in ACHIEVEMENTS
            else next(
                (key for key, value in ACHIEVEMENTS.items() if value["title"] == title),
                "",
            )
        )
        if not title_id:
            raise ValueError("未知称号")
        if title_id not in self._list_achievements_sync(user_id):
            raise ValueError("该称号尚未解锁")
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE checkin_users SET selected_title_id = ?, updated_at = ? WHERE user_id = ?",
                (title_id, self.now_iso(), user_id),
            )
            conn.commit()
        return title_id

    def _list_owned_theme_ids_sync(self, user_id: str) -> tuple[str, ...]:
        if not user_id:
            return ()
        self._get_or_create_preference_sync(user_id)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT owned.theme_id
                FROM checkin_user_themes AS owned
                JOIN checkin_themes AS theme ON theme.theme_id = owned.theme_id
                WHERE owned.user_id = ? AND theme.enabled = 1
                ORDER BY theme.sort_order, owned.acquired_at
                """,
                (user_id,),
            ).fetchall()
        return tuple(str(row["theme_id"]) for row in rows)

    def _purchase_theme_sync(
        self,
        user_id: str,
        theme_id: str,
        configured_cost: int | None = None,
    ) -> ThemePurchaseResult:
        if not user_id:
            raise ValueError("user_id is required")
        if not theme_id or theme_id == "default":
            raise ValueError("默认主题无需购买")
        now = self.now_iso()
        date_key = self.today_key()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO checkin_users (
                        user_id, coins, affection, total_days, streak_days,
                        last_checkin_date, boost_start_date, boost_until_date,
                        repeat_penalty_date, repeat_penalty_total,
                        created_at, updated_at
                    )
                    VALUES (?, 0, 0, 0, 0, '', '', '', '', 0, ?, ?)
                    """,
                    (user_id, now, now),
                )
                conn.execute(
                    """
                    INSERT OR IGNORE INTO checkin_user_themes
                        (user_id, theme_id, price_paid, acquired_at)
                    VALUES (?, 'default', 0, ?)
                    """,
                    (user_id, now),
                )
                theme_row = conn.execute(
                    """
                    SELECT price, version FROM checkin_themes
                    WHERE theme_id = ? AND enabled = 1
                    """,
                    (theme_id,),
                ).fetchone()
                if theme_row is None:
                    raise ValueError("未知或已停用的主题")
                cost = (
                    int(theme_row["price"] or 0)
                    if configured_cost is None
                    else configured_cost
                )
                template_version = f"{theme_id}:{int(theme_row['version'] or 1)}"
                profile_row = conn.execute(
                    "SELECT * FROM checkin_users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                existing = conn.execute(
                    "SELECT 1 FROM checkin_user_themes "
                    "WHERE user_id = ? AND theme_id = ?",
                    (user_id, theme_id),
                ).fetchone()
                if existing is not None:
                    conn.execute(
                        "UPDATE checkin_users "
                        "SET current_theme_id = ?, updated_at = ? WHERE user_id = ?",
                        (theme_id, now, user_id),
                    )
                    conn.execute(
                        """
                        UPDATE checkin_records
                        SET theme_id = ?, template_version = ?, updated_at = ?
                        WHERE date_key = ? AND user_id = ?
                        """,
                        (theme_id, template_version, now, date_key, user_id),
                    )
                    conn.commit()
                    return ThemePurchaseResult(
                        True,
                        self._row_to_profile(profile_row),
                        theme_id,
                        0,
                        True,
                        "该主题已经购买，已为你切换。",
                    )
                coins = int(profile_row["coins"] or 0)
                if coins < cost:
                    conn.commit()
                    return ThemePurchaseResult(
                        False,
                        self._row_to_profile(profile_row),
                        theme_id,
                        cost,
                        False,
                        f"金币不足，需要 {cost}，当前只有 {coins}。",
                    )
                remaining = coins - cost
                conn.execute(
                    "UPDATE checkin_users SET coins = ?, updated_at = ? "
                    "WHERE user_id = ?",
                    (remaining, now, user_id),
                )
                conn.execute(
                    """
                    INSERT INTO checkin_user_themes
                    (user_id, theme_id, price_paid, acquired_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, theme_id, cost, now),
                )
                conn.execute(
                    "UPDATE checkin_users "
                    "SET current_theme_id = ?, updated_at = ? WHERE user_id = ?",
                    (theme_id, now, user_id),
                )
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET theme_id = ?, template_version = ?,
                        total_coins_after = ?, updated_at = ?
                    WHERE date_key = ? AND user_id = ?
                    """,
                    (
                        theme_id,
                        template_version,
                        remaining,
                        now,
                        date_key,
                        user_id,
                    ),
                )
                updated_row = conn.execute(
                    "SELECT * FROM checkin_users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ThemePurchaseResult(
            True,
            self._row_to_profile(updated_row),
            theme_id,
            cost,
            False,
            f"购买成功，消耗 {cost} 金币并已切换主题。",
        )

    def _select_theme_sync(self, user_id: str, theme_id: str) -> str:
        if not user_id:
            raise ValueError("user_id is required")
        if not theme_id:
            raise ValueError("theme_id is required")
        self._get_or_create_preference_sync(user_id)
        now = self.now_iso()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            theme_row = conn.execute(
                "SELECT version FROM checkin_themes WHERE theme_id = ? AND enabled = 1",
                (theme_id,),
            ).fetchone()
            if theme_row is None:
                raise ValueError("未知或已停用的主题")
            owned = conn.execute(
                "SELECT 1 FROM checkin_user_themes WHERE user_id = ? AND theme_id = ?",
                (user_id, theme_id),
            ).fetchone()
            if owned is None:
                raise ValueError("该主题尚未购买")
            template_version = f"{theme_id}:{int(theme_row['version'] or 1)}"
            conn.execute(
                "UPDATE checkin_users "
                "SET current_theme_id = ?, updated_at = ? WHERE user_id = ?",
                (theme_id, now, user_id),
            )
            conn.execute(
                """
                UPDATE checkin_records
                SET theme_id = ?, template_version = ?, updated_at = ?
                WHERE date_key = ? AND user_id = ?
                """,
                (theme_id, template_version, now, self.today_key(), user_id),
            )
            conn.commit()
        return theme_id

    def _add_global_event_sync(
        self, event_type: str, date_value: str, name: str, created_by: str
    ) -> CheckinGlobalEvent:
        now = self.now_iso()
        with closing(self._connect()) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO checkin_global_events
                    (event_type, date_value, name, created_by, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (event_type, date_value, name, created_by, now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError("该日期已存在同类型事件") from exc
            row = conn.execute(
                "SELECT * FROM checkin_global_events WHERE event_id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return self._row_to_global_event(row)

    def _list_global_events_sync(self) -> tuple[CheckinGlobalEvent, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM checkin_global_events ORDER BY event_type, date_value, event_id"
            ).fetchall()
        return tuple(self._row_to_global_event(row) for row in rows)

    def _delete_global_event_sync(self, event_id: int) -> bool:
        with closing(self._connect()) as conn:
            changed = conn.execute(
                "DELETE FROM checkin_global_events WHERE event_id = ?", (event_id,)
            ).rowcount
            conn.commit()
        return bool(changed)

    def _events_for_date_sync(
        self, exact_date: str, annual_date: str
    ) -> tuple[CheckinGlobalEvent, ...]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM checkin_global_events
                WHERE (event_type = 'once' AND date_value = ?)
                   OR (event_type = 'annual' AND date_value = ?)
                ORDER BY CASE event_type WHEN 'once' THEN 0 ELSE 1 END, event_id
                """,
                (exact_date, annual_date),
            ).fetchall()
        return tuple(self._row_to_global_event(row) for row in rows)

    @staticmethod
    def _row_to_preference(row: sqlite3.Row) -> CheckinUserPreference:
        return CheckinUserPreference(
            user_id=str(row["user_id"]),
            birthday_month=int(row["birthday_month"] or 0),
            birthday_day=int(row["birthday_day"] or 0),
            birthday_source=str(row["birthday_source"] or ""),
            qq_birthday_checked=bool(row["qq_birthday_checked"]),
            selected_title_id=str(row["selected_title_id"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            current_theme_id=str(row["current_theme_id"] or "default"),
        )

    @staticmethod
    def _row_to_global_event(row: sqlite3.Row) -> CheckinGlobalEvent:
        return CheckinGlobalEvent(
            event_id=int(row["event_id"]),
            event_type=str(row["event_type"]),
            date_value=str(row["date_value"]),
            name=str(row["name"]),
            created_by=str(row["created_by"] or ""),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
