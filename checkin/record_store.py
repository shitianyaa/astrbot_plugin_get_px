from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import date, timedelta
import math
import sqlite3

from .models import (
    BOOST_MULTIPLIER,
    BOOST_PRODUCTS,
    STREAK_AFFECTION_BONUS,
    STREAK_AFFECTION_BONUS_MAX,
    STREAK_COIN_BONUS,
    STREAK_COIN_BONUS_MAX,
    STREAK_STEP_DAYS,
    BackgroundRefreshResult,
    BoostPurchaseResult,
    CheckinProfile,
    CheckinRecord,
    CheckinResult,
)
from .rules import (
    daily_base_reward as _daily_base_reward,
    daily_note as _daily_note,
    is_boost_active,
    parse_date as _parse_date,
)
from .snapshot import validate_greeting_source as _validate_greeting_source


_MAX_PROFILE_INTEGER = 2_147_483_647
_MIN_PROFILE_AFFECTION = -10.0
_MAX_PROFILE_AFFECTION = 1_000_000.0
_MEMBER_SELECT = """
    SELECT p.*,
        COALESCE(
            NULLIF((
                SELECT recent_name.username
                FROM (
                    SELECT r.username, r.updated_at AS seen_at, 0 AS source_priority
                    FROM checkin_records AS r
                    WHERE r.user_id = p.user_id AND TRIM(r.username) != ''
                    UNION ALL
                    SELECT g.username, g.last_seen_at AS seen_at, 1 AS source_priority
                    FROM checkin_group_presence AS g
                    WHERE g.user_id = p.user_id AND TRIM(g.username) != ''
                ) AS recent_name
                ORDER BY recent_name.seen_at DESC, recent_name.source_priority DESC
                LIMIT 1
            ), ''),
            p.user_id
        ) AS username
    FROM checkin_profiles AS p
"""


class RecordStoreMixin:
    async def find_profile(self, user_id: str) -> CheckinProfile | None:
        user_id = str(user_id or "")
        if not user_id:
            return None
        async with self._lock:
            return await asyncio.to_thread(self._find_profile_sync, user_id)

    async def get_profile(self, user_id: str) -> CheckinProfile:
        user_id = str(user_id or "")
        async with self._lock:
            return await asyncio.to_thread(self._get_or_create_profile_sync, user_id)

    async def list_checkin_members(
        self,
        *,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        query = str(query or "").strip()
        if len(query) > 64:
            raise ValueError("搜索内容不能超过 64 个字符")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if not 0 <= offset <= 1_000_000:
            raise ValueError("offset must be between 0 and 1000000")
        async with self._lock:
            return await asyncio.to_thread(
                self._list_checkin_members_sync,
                query,
                int(limit),
                int(offset),
            )

    async def update_checkin_member(
        self,
        *,
        user_id: str,
        coins: int,
        affection: float,
        total_days: int,
        streak_days: int,
    ) -> dict[str, dict[str, object]]:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("缺少用户 ID")
        if len(user_id) > 128:
            raise ValueError("用户 ID 不能超过 128 个字符")
        integer_values = {
            "金币": coins,
            "累计签到": total_days,
            "连续签到": streak_days,
        }
        for label, value in integer_values.items():
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label}必须是整数")
            if not 0 <= value <= _MAX_PROFILE_INTEGER:
                raise ValueError(f"{label}必须在 0 至 {_MAX_PROFILE_INTEGER} 之间")
        if isinstance(affection, bool) or not isinstance(affection, (int, float)):
            raise ValueError("好感度必须是数字")
        affection = float(affection)
        if not math.isfinite(affection):
            raise ValueError("好感度必须是有限数字")
        if not _MIN_PROFILE_AFFECTION <= affection <= _MAX_PROFILE_AFFECTION:
            raise ValueError(
                f"好感度必须在 {_MIN_PROFILE_AFFECTION:g} 至 "
                f"{_MAX_PROFILE_AFFECTION:g} 之间"
            )
        if streak_days > total_days:
            raise ValueError("连续签到不能大于累计签到")
        async with self._lock:
            return await asyncio.to_thread(
                self._update_checkin_member_sync,
                user_id,
                coins,
                round(affection, 2),
                total_days,
                streak_days,
                self.now_iso(),
            )

    async def get_today_record(self, user_id: str) -> CheckinRecord | None:
        user_id = str(user_id or "")
        date_key = self.today_key()
        async with self._lock:
            return await asyncio.to_thread(
                self._get_record_sync, date_key=date_key, user_id=user_id
            )

    async def checkin(
        self,
        *,
        user_id: str,
        username: str,
        bot_name: str,
        theme_id: str = "default",
        template_version: str = "v2",
        group_id: str = "",
        group_name: str = "",
        platform: str = "",
    ) -> CheckinResult:
        user_id = str(user_id or "")
        if not user_id:
            raise ValueError("user_id is required")
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._checkin_sync,
                date_key,
                now,
                user_id,
                str(username or user_id),
                str(bot_name or "neko"),
                str(theme_id or "default"),
                str(template_version or "v2"),
                str(group_id or ""),
                str(group_name or ""),
                str(platform or ""),
            )

    async def purchase_boost(self, *, user_id: str, days: int) -> BoostPurchaseResult:
        user_id = str(user_id or "")
        if days not in BOOST_PRODUCTS:
            profile = await self.get_profile(user_id)
            return BoostPurchaseResult(
                success=False,
                profile=profile,
                days=days,
                cost=0,
                message="可购买的加持天数只有 1、3、7 天。",
            )
        date_key = self.today_key()
        now = self.now_iso()
        async with self._lock:
            return await asyncio.to_thread(
                self._purchase_boost_sync, date_key, now, user_id, days
            )

    async def update_record_background(
        self,
        *,
        user_id: str,
        date_key: str,
        mode: str,
        source: str = "",
        illust_id: str = "",
        title: str = "",
        author: str = "",
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_record_background_sync,
                str(user_id or ""),
                str(date_key or ""),
                str(mode or ""),
                str(source or ""),
                str(illust_id or ""),
                str(title or ""),
                str(author or ""),
                self.now_iso(),
            )

    async def purchase_background_refresh(
        self,
        *,
        user_id: str,
        cost: int,
        mode: str,
        source: str,
        illust_id: str,
        title: str,
        author: str,
    ) -> BackgroundRefreshResult:
        async with self._lock:
            return await asyncio.to_thread(
                self._purchase_background_refresh_sync,
                str(user_id or ""),
                max(0, int(cost)),
                str(mode or ""),
                str(source or ""),
                str(illust_id or ""),
                str(title or ""),
                str(author or ""),
                self.today_key(),
                self.now_iso(),
            )

    async def update_record_content(
        self,
        *,
        user_id: str,
        date_key: str,
        event_key: str,
        event_label: str,
        greeting: str,
        greeting_source: str,
        secondary_note: str,
        template_version: str,
        greeting_attribution: str = "",
    ) -> CheckinRecord:
        """Persist local content once and permit only one remote-source upgrade."""
        validated_source = _validate_greeting_source(greeting_source, "greeting_source")
        async with self._lock:
            return await asyncio.to_thread(
                self._update_record_content_sync,
                str(user_id or ""),
                str(date_key or ""),
                str(event_key or ""),
                str(event_label or ""),
                str(greeting or ""),
                validated_source,
                str(secondary_note or ""),
                str(template_version or "v2"),
                str(greeting_attribution or ""),
                self.now_iso(),
            )

    async def refresh_record_greeting(
        self,
        *,
        user_id: str,
        date_key: str,
        greeting: str,
        greeting_source: str,
        greeting_attribution: str = "",
    ) -> CheckinRecord:
        """Refresh an already remote-generated greeting without reopening content upgrades."""
        validated_source = _validate_greeting_source(greeting_source, "greeting_source")
        if validated_source not in {"ai", "hitokoto"}:
            raise ValueError("greeting_source must be ai or hitokoto")
        async with self._lock:
            return await asyncio.to_thread(
                self._refresh_record_greeting_sync,
                str(user_id or ""),
                str(date_key or ""),
                str(greeting or ""),
                validated_source,
                str(greeting_attribution or ""),
                self.now_iso(),
            )

    def _get_or_create_profile_sync(self, user_id: str) -> CheckinProfile:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row is not None:
                return self._row_to_profile(row)
            now = self.now_iso()
            conn.execute(
                """
                INSERT INTO checkin_profiles (
                    user_id, coins, affection, total_days, streak_days,
                    last_checkin_date, boost_start_date, boost_until_date,
                    repeat_penalty_date, repeat_penalty_total, created_at, updated_at
                )
                VALUES (?, 0, 0, 0, 0, '', '', '', '', 0, ?, ?)
                """,
                (user_id, now, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._row_to_profile(row)

    def _find_profile_sync(self, user_id: str) -> CheckinProfile | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        return self._row_to_profile(row) if row is not None else None

    def _list_checkin_members_sync(
        self,
        query: str,
        limit: int,
        offset: int,
    ) -> dict[str, object]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        where_sql = """
            WHERE ? = ''
                OR p.user_id LIKE ? ESCAPE '\\'
                OR EXISTS (
                    SELECT 1 FROM checkin_records AS r
                    WHERE r.user_id = p.user_id
                        AND r.username LIKE ? ESCAPE '\\'
                )
                OR EXISTS (
                    SELECT 1 FROM checkin_group_presence AS g
                    WHERE g.user_id = p.user_id
                        AND g.username LIKE ? ESCAPE '\\'
                )
        """
        params = (query, pattern, pattern, pattern)
        with closing(self._connect()) as conn:
            total_row = conn.execute(
                f"SELECT COUNT(*) AS count FROM checkin_profiles AS p {where_sql}",
                params,
            ).fetchone()
            rows = conn.execute(
                f"""
                {_MEMBER_SELECT}
                {where_sql}
                ORDER BY p.updated_at DESC, p.user_id ASC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return {
            "total": int(total_row["count"] or 0),
            "members": [self._row_to_member(row) for row in rows],
        }

    def _update_checkin_member_sync(
        self,
        user_id: str,
        coins: int,
        affection: float,
        total_days: int,
        streak_days: int,
        now: str,
    ) -> dict[str, dict[str, object]]:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                before_row = conn.execute(
                    f"{_MEMBER_SELECT} WHERE p.user_id = ?", (user_id,)
                ).fetchone()
                if before_row is None:
                    raise LookupError("指定成员不存在")
                conn.execute(
                    """
                    UPDATE checkin_profiles
                    SET coins = ?, affection = ?, total_days = ?, streak_days = ?,
                        updated_at = ?
                    WHERE user_id = ?
                    """,
                    (coins, affection, total_days, streak_days, now, user_id),
                )
                after_row = conn.execute(
                    f"{_MEMBER_SELECT} WHERE p.user_id = ?", (user_id,)
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "before": self._row_to_member(before_row),
            "member": self._row_to_member(after_row),
        }

    def _checkin_sync(
        self,
        date_key: str,
        now: str,
        user_id: str,
        username: str,
        bot_name: str,
        theme_id: str,
        template_version: str,
        group_id: str,
        group_name: str,
        platform: str,
    ) -> CheckinResult:
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO checkin_profiles (
                        user_id, coins, affection, total_days, streak_days,
                        last_checkin_date, boost_start_date, boost_until_date,
                        repeat_penalty_date, repeat_penalty_total,
                        created_at, updated_at
                    )
                    VALUES (?, 0, 0, 0, 0, '', '', '', '', 0, ?, ?)
                    """,
                    (user_id, now, now),
                )
                profile_row = conn.execute(
                    "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                profile = self._row_to_profile(profile_row)
                record_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE date_key = ? AND user_id = ?
                    """,
                    (date_key, user_id),
                ).fetchone()
                if group_id:
                    conn.execute(
                        """
                        INSERT INTO checkin_group_presence (
                            date_key, group_id, group_name, platform, user_id,
                            username, first_seen_at, last_seen_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(date_key, group_id, user_id) DO UPDATE SET
                            group_name = CASE WHEN excluded.group_name != ''
                                THEN excluded.group_name
                                ELSE checkin_group_presence.group_name END,
                            platform = CASE WHEN excluded.platform != ''
                                THEN excluded.platform
                                ELSE checkin_group_presence.platform END,
                            username = excluded.username,
                            last_seen_at = excluded.last_seen_at
                        """,
                        (
                            date_key,
                            group_id,
                            group_name,
                            platform,
                            user_id,
                            username,
                            now,
                            now,
                        ),
                    )
                if record_row is not None:
                    conn.commit()
                    return CheckinResult(
                        profile=profile,
                        record=self._row_to_record(record_row),
                        duplicate=True,
                    )

                previous_date = _parse_date(profile.last_checkin_date)
                today = date.fromisoformat(date_key)
                if previous_date == today - timedelta(days=1):
                    streak_days = profile.streak_days + 1
                else:
                    streak_days = 1

                total_days = profile.total_days + 1
                base_coins, base_affection = _daily_base_reward(user_id, date_key)
                streak_steps = streak_days // STREAK_STEP_DAYS
                bonus_coins = min(
                    streak_steps * STREAK_COIN_BONUS, STREAK_COIN_BONUS_MAX
                )
                bonus_affection = min(
                    streak_steps * STREAK_AFFECTION_BONUS,
                    STREAK_AFFECTION_BONUS_MAX,
                )
                boost_active = is_boost_active(profile, date_key)
                affection_reward = round(base_affection + bonus_affection, 2)
                if boost_active:
                    affection_reward = round(affection_reward * BOOST_MULTIPLIER, 2)
                coins_reward = base_coins + bonus_coins
                new_coins = profile.coins + coins_reward
                new_affection = round(profile.affection + affection_reward, 2)
                note = _daily_note(user_id, date_key, streak_days)

                conn.execute(
                    """
                    UPDATE checkin_profiles
                    SET coins = ?, affection = ?, total_days = ?, streak_days = ?,
                        last_checkin_date = ?, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        new_coins,
                        new_affection,
                        total_days,
                        streak_days,
                        date_key,
                        now,
                        user_id,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO checkin_records (
                        date_key, user_id, username, bot_name,
                        base_coins, bonus_coins, coins_reward,
                        base_affection, bonus_affection, affection_reward,
                        boost_active, boost_multiplier,
                        total_coins_after, total_affection_after,
                        total_days_after, streak_days_after,
                        note, theme_id, template_version, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        date_key,
                        user_id,
                        username,
                        bot_name,
                        base_coins,
                        bonus_coins,
                        coins_reward,
                        base_affection,
                        bonus_affection,
                        affection_reward,
                        1 if boost_active else 0,
                        BOOST_MULTIPLIER if boost_active else 1.0,
                        new_coins,
                        new_affection,
                        total_days,
                        streak_days,
                        note,
                        theme_id,
                        template_version,
                        now,
                        now,
                    ),
                )
                updated_row = conn.execute(
                    "SELECT * FROM checkin_profiles WHERE user_id = ?", (user_id,)
                ).fetchone()
                record_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE date_key = ? AND user_id = ?
                    """,
                    (date_key, user_id),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        return CheckinResult(
            profile=self._row_to_profile(updated_row),
            record=self._row_to_record(record_row),
            duplicate=False,
        )

    def _purchase_boost_sync(
        self, date_key: str, now: str, user_id: str, days: int
    ) -> BoostPurchaseResult:
        cost = BOOST_PRODUCTS[days]
        profile = self._get_or_create_profile_sync(user_id)
        if profile.coins < cost:
            return BoostPurchaseResult(
                success=False,
                profile=profile,
                days=days,
                cost=cost,
                message=f"金币不足，需要 {cost}，当前只有 {profile.coins}。",
            )

        today = date.fromisoformat(date_key)
        signed_today = profile.last_checkin_date == date_key
        requested_start = today + timedelta(days=1 if signed_today else 0)
        current_until = _parse_date(profile.boost_until_date)
        current_start = _parse_date(profile.boost_start_date)
        if current_until is not None and current_until >= requested_start:
            start_date = current_start or requested_start
            until_date = current_until + timedelta(days=days)
        else:
            start_date = requested_start
            until_date = requested_start + timedelta(days=days - 1)

        with closing(self._connect()) as conn:
            remaining = profile.coins - cost
            conn.execute(
                """
                UPDATE checkin_profiles
                SET coins = ?, boost_start_date = ?, boost_until_date = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    remaining,
                    start_date.isoformat(),
                    until_date.isoformat(),
                    now,
                    user_id,
                ),
            )
            conn.execute(
                """
                UPDATE checkin_records
                SET total_coins_after = ?, updated_at = ?
                WHERE date_key = ? AND user_id = ?
                """,
                (remaining, now, date_key, user_id),
            )
            conn.commit()

        updated = self._get_or_create_profile_sync(user_id)
        start_label = "今天" if start_date == today else start_date.isoformat()
        message = (
            f"购买成功，消耗 {cost} 金币。好感度双倍加持从 {start_label} "
            f"生效，到 {until_date.isoformat()} 结束。"
        )
        return BoostPurchaseResult(True, updated, days, cost, message)

    def _purchase_background_refresh_sync(
        self,
        user_id: str,
        cost: int,
        mode: str,
        source: str,
        illust_id: str,
        title: str,
        author: str,
        date_key: str,
        now: str,
    ) -> BackgroundRefreshResult:
        if not user_id:
            raise ValueError("user_id is required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                profile_row = conn.execute(
                    "SELECT * FROM checkin_profiles WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                record_row = conn.execute(
                    "SELECT * FROM checkin_records WHERE date_key = ? AND user_id = ?",
                    (date_key, user_id),
                ).fetchone()
                if profile_row is None or record_row is None:
                    conn.commit()
                    profile = (
                        self._row_to_profile(profile_row)
                        if profile_row is not None
                        else self._get_or_create_profile_sync(user_id)
                    )
                    return BackgroundRefreshResult(
                        False, profile, None, cost, "请先完成今天的签到。"
                    )
                profile = self._row_to_profile(profile_row)
                if profile.coins < cost:
                    conn.commit()
                    return BackgroundRefreshResult(
                        False,
                        profile,
                        self._row_to_record(record_row),
                        cost,
                        f"金币不足，需要 {cost}，当前只有 {profile.coins}。",
                    )
                remaining = profile.coins - cost
                conn.execute(
                    "UPDATE checkin_profiles SET coins = ?, updated_at = ? "
                    "WHERE user_id = ?",
                    (remaining, now, user_id),
                )
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET background_mode = ?, background_source = ?,
                        background_illust_id = ?, background_title = ?,
                        background_author = ?, total_coins_after = ?, updated_at = ?
                    WHERE date_key = ? AND user_id = ?
                    """,
                    (
                        mode,
                        source,
                        illust_id,
                        title,
                        author,
                        remaining,
                        now,
                        date_key,
                        user_id,
                    ),
                )
                updated_profile_row = conn.execute(
                    "SELECT * FROM checkin_profiles WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                updated_record_row = conn.execute(
                    "SELECT * FROM checkin_records WHERE date_key = ? AND user_id = ?",
                    (date_key, user_id),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return BackgroundRefreshResult(
            True,
            self._row_to_profile(updated_profile_row),
            self._row_to_record(updated_record_row),
            cost,
            f"背景已更新，消耗 {cost} 金币。",
        )

    def _get_record_sync(self, *, date_key: str, user_id: str) -> CheckinRecord | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM checkin_records
                WHERE date_key = ? AND user_id = ?
                """,
                (date_key, user_id),
            ).fetchone()
        return self._row_to_record(row) if row is not None else None

    def _update_record_background_sync(
        self,
        user_id: str,
        date_key: str,
        mode: str,
        source: str,
        illust_id: str,
        title: str,
        author: str,
        now: str,
    ) -> None:
        if not user_id or not date_key:
            return
        with closing(self._connect()) as conn:
            conn.execute(
                """
                UPDATE checkin_records
                SET background_mode = ?, background_source = ?,
                    background_illust_id = ?, background_title = ?,
                    background_author = ?, updated_at = ?
                WHERE user_id = ? AND date_key = ?
                """,
                (mode, source, illust_id, title, author, now, user_id, date_key),
            )
            conn.commit()

    def _update_record_content_sync(
        self,
        user_id: str,
        date_key: str,
        event_key: str,
        event_label: str,
        greeting: str,
        greeting_source: str,
        secondary_note: str,
        template_version: str,
        greeting_attribution: str,
        now: str,
    ) -> CheckinRecord:
        if not user_id or not date_key:
            raise ValueError("user_id and date_key are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET event_key = ?, event_label = ?, greeting = ?,
                        greeting_source = ?, greeting_attribution = ?,
                        secondary_note = ?,
                        template_version = ?, updated_at = ?
                    WHERE user_id = ? AND date_key = ?
                      AND NOT (? = '' AND ? = '' AND ? = '' AND ? = '')
                      AND (
                        (
                            ? = 'local'
                            AND greeting_source = 'local'
                            AND event_key = '' AND event_label = ''
                            AND greeting = '' AND secondary_note = ''
                        )
                        OR
                        (
                            ? IN ('ai', 'hitokoto') AND greeting_source = 'local'
                            AND NOT (
                                event_key = '' AND event_label = ''
                                AND greeting = '' AND secondary_note = ''
                            )
                        )
                      )
                    """,
                    (
                        event_key,
                        event_label,
                        greeting,
                        greeting_source,
                        greeting_attribution,
                        secondary_note,
                        template_version,
                        now,
                        user_id,
                        date_key,
                        event_key,
                        event_label,
                        greeting,
                        secondary_note,
                        greeting_source,
                        greeting_source,
                    ),
                )
                updated_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE user_id = ? AND date_key = ?
                    """,
                    (user_id, date_key),
                ).fetchone()
                if updated_row is None:
                    raise ValueError("check-in record not found")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._row_to_record(updated_row)

    def _refresh_record_greeting_sync(
        self,
        user_id: str,
        date_key: str,
        greeting: str,
        greeting_source: str,
        greeting_attribution: str,
        now: str,
    ) -> CheckinRecord:
        if not user_id or not date_key:
            raise ValueError("user_id and date_key are required")
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET greeting = ?, greeting_source = ?,
                        greeting_attribution = ?, updated_at = ?
                    WHERE user_id = ? AND date_key = ?
                      AND greeting_source IN ('local', 'ai', 'hitokoto')
                    """,
                    (
                        greeting,
                        greeting_source,
                        greeting_attribution,
                        now,
                        user_id,
                        date_key,
                    ),
                )
                updated_row = conn.execute(
                    """
                    SELECT * FROM checkin_records
                    WHERE user_id = ? AND date_key = ?
                    """,
                    (user_id, date_key),
                ).fetchone()
                if updated_row is None:
                    raise ValueError("check-in record not found")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return self._row_to_record(updated_row)

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> CheckinProfile:
        return CheckinProfile(
            user_id=str(row["user_id"]),
            coins=int(row["coins"] or 0),
            affection=round(float(row["affection"] or 0), 2),
            total_days=int(row["total_days"] or 0),
            streak_days=int(row["streak_days"] or 0),
            last_checkin_date=str(row["last_checkin_date"] or ""),
            boost_start_date=str(row["boost_start_date"] or ""),
            boost_until_date=str(row["boost_until_date"] or ""),
            repeat_penalty_date=str(row["repeat_penalty_date"] or ""),
            repeat_penalty_total=round(float(row["repeat_penalty_total"] or 0), 2),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
        )

    @staticmethod
    def _row_to_member(row: sqlite3.Row) -> dict[str, object]:
        return {
            "user_id": str(row["user_id"]),
            "username": str(row["username"] or row["user_id"]),
            "coins": int(row["coins"] or 0),
            "affection": round(float(row["affection"] or 0), 2),
            "total_days": int(row["total_days"] or 0),
            "streak_days": int(row["streak_days"] or 0),
            "last_checkin_date": str(row["last_checkin_date"] or ""),
            "boost_until_date": str(row["boost_until_date"] or ""),
            "updated_at": str(row["updated_at"] or ""),
        }

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CheckinRecord:
        return CheckinRecord(
            date_key=str(row["date_key"]),
            user_id=str(row["user_id"]),
            username=str(row["username"] or ""),
            bot_name=str(row["bot_name"] or ""),
            base_coins=int(row["base_coins"] or 0),
            bonus_coins=int(row["bonus_coins"] or 0),
            coins_reward=int(row["coins_reward"] or 0),
            base_affection=round(float(row["base_affection"] or 0), 2),
            bonus_affection=round(float(row["bonus_affection"] or 0), 2),
            affection_reward=round(float(row["affection_reward"] or 0), 2),
            boost_active=bool(row["boost_active"]),
            boost_multiplier=round(float(row["boost_multiplier"] or 1), 2),
            total_coins_after=int(row["total_coins_after"] or 0),
            total_affection_after=round(float(row["total_affection_after"] or 0), 2),
            total_days_after=int(row["total_days_after"] or 0),
            streak_days_after=int(row["streak_days_after"] or 0),
            note=str(row["note"] or ""),
            background_mode=str(row["background_mode"] or ""),
            background_source=str(row["background_source"] or ""),
            background_illust_id=str(row["background_illust_id"] or ""),
            background_title=str(row["background_title"] or ""),
            background_author=str(row["background_author"] or ""),
            created_at=str(row["created_at"] or ""),
            updated_at=str(row["updated_at"] or ""),
            event_key=str(row["event_key"] or ""),
            event_label=str(row["event_label"] or ""),
            greeting=str(row["greeting"] or ""),
            greeting_source=str(row["greeting_source"] or "local"),
            greeting_attribution=str(row["greeting_attribution"] or ""),
            secondary_note=str(row["secondary_note"] or ""),
            template_version=str(row["template_version"] or "v2"),
            theme_id=str(row["theme_id"] or "default"),
        )
