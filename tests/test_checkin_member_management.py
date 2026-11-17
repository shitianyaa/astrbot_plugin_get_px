from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
import sqlite3
import tempfile

import pytest

from checkin import CheckinStore


FIXED_DATE = "2026-05-26"


class FrozenCheckinStore(CheckinStore):
    """Pin today_key to a fixed date so boost assertions don't flak at midnight."""

    def today_key(self) -> str:
        return FIXED_DATE

    def now_iso(self) -> str:
        return f"{FIXED_DATE}T12:00:00+08:00"


@pytest.mark.asyncio
async def test_member_search_update_and_history_isolation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp)
        alice = await store.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
        )
        await store.checkin(
            user_id="10002",
            username="Bob",
            bot_name="neko",
        )
        await store.checkin(
            user_id="10001",
            username="Alice New",
            bot_name="neko",
            group_id="778899",
            group_name="测试群",
            platform="aiocqhttp",
        )

        listing = await store.list_checkin_members(limit=1, offset=0)
        assert listing["total"] == 2
        assert len(listing["members"]) == 1

        by_name = await store.list_checkin_members(
            query="Alice New", limit=50, offset=0
        )
        assert [item["user_id"] for item in by_name["members"]] == ["10001"]
        assert by_name["members"][0]["username"] == "Alice New"

        by_id = await store.list_checkin_members(query="10002", limit=50, offset=0)
        assert [item["username"] for item in by_id["members"]] == ["Bob"]

        result = await store.update_checkin_member(
            user_id="10001",
            coins=500,
            affection=-5.25,
            total_days=12,
            streak_days=4,
            boost_action="add_3",
        )
        assert result["before"]["coins"] == alice.profile.coins
        assert result["member"]["coins"] == 500
        assert result["member"]["affection"] == -5.25
        assert result["member"]["total_days"] == 12
        assert result["member"]["streak_days"] == 4
        # Alice 已在今天签到，加持从明天起算，今天 boost_active 为 False
        assert result["member"]["boost_active"] is False
        assert result["member"]["boost_remaining_days"] >= 3
        assert "生效" in str(result["member"]["boost_status"])

        cleared = await store.update_checkin_member(
            user_id="10001",
            coins=500,
            affection=-5.25,
            total_days=12,
            streak_days=4,
            boost_action="clear",
        )
        assert cleared["member"]["boost_active"] is False
        assert cleared["member"]["boost_remaining_days"] == 0
        assert cleared["member"]["boost_status"] == "无加持"

        profile = await store.get_profile("10001")
        assert profile.coins == 500
        assert profile.affection == -5.25
        assert profile.total_days == 12
        assert profile.streak_days == 4

        historical = await store.get_today_record("10001")
        assert historical is not None
        assert historical.total_coins_after == alice.record.total_coins_after
        assert historical.total_days_after == alice.record.total_days_after

        with closing(sqlite3.connect(store._db_path)) as conn:
            indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(checkin_records)")
            }
            presence_indexes = {
                row[1]
                for row in conn.execute("PRAGMA index_list(checkin_group_presence)")
            }
        assert "idx_checkin_records_member_updated" in indexes
        assert "idx_checkin_group_presence_member_seen" in presence_indexes


@pytest.mark.asyncio
async def test_member_update_rejects_invalid_values_and_unknown_users() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        await store.get_profile("10001")

        with pytest.raises(ValueError, match="连续签到不能大于累计签到"):
            await store.update_checkin_member(
                user_id="10001",
                coins=0,
                affection=0,
                total_days=2,
                streak_days=3,
            )

        with pytest.raises(ValueError, match="好感度"):
            await store.update_checkin_member(
                user_id="10001",
                coins=0,
                affection=-10.01,
                total_days=0,
                streak_days=0,
            )

        with pytest.raises(LookupError, match="成员不存在"):
            await store.update_checkin_member(
                user_id="404",
                coins=0,
                affection=0,
                total_days=0,
                streak_days=0,
            )

        with pytest.raises(ValueError, match="加持操作"):
            await store.update_checkin_member(
                user_id="10001",
                coins=0,
                affection=0,
                total_days=0,
                streak_days=0,
                boost_action="invalid_action",
            )


@pytest.mark.asyncio
async def test_member_boost_extend_active_and_unsigned_today() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp)
        # 未签到的用户：加持从今天起算，boost_active 为 True
        await store.get_profile("20001")
        first = await store.update_checkin_member(
            user_id="20001",
            coins=0,
            affection=0,
            total_days=0,
            streak_days=0,
            boost_action="add_3",
        )
        assert first["member"]["boost_active"] is True
        assert first["member"]["boost_remaining_days"] >= 3
        first_until = first["member"]["boost_until_date"]
        assert first_until != ""

        # 在已生效加持基础上再延长 1 天：until 日期应顺延 1 天
        extended = await store.update_checkin_member(
            user_id="20001",
            coins=0,
            affection=0,
            total_days=0,
            streak_days=0,
            boost_action="add_1",
        )
        assert extended["member"]["boost_active"] is True
        prev = date.fromisoformat(first_until)
        expected = (prev + timedelta(days=1)).isoformat()
        assert extended["member"]["boost_until_date"] == expected
