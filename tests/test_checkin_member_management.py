from __future__ import annotations

import tempfile

import pytest

from checkin import CheckinStore


@pytest.mark.asyncio
async def test_member_search_update_and_history_isolation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
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
        )
        assert result["before"]["coins"] == alice.profile.coins
        assert result["member"]["coins"] == 500
        assert result["member"]["affection"] == -5.25
        assert result["member"]["total_days"] == 12
        assert result["member"]["streak_days"] == 4

        profile = await store.get_profile("10001")
        assert profile.coins == 500
        assert profile.affection == -5.25
        assert profile.total_days == 12
        assert profile.streak_days == 4

        historical = await store.get_today_record("10001")
        assert historical is not None
        assert historical.total_coins_after == alice.record.total_coins_after
        assert historical.total_days_after == alice.record.total_days_after


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
