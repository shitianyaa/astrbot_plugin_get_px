from __future__ import annotations

import tempfile

import pytest

from checkin import CheckinStore


class FrozenCheckinStore(CheckinStore):
    def __init__(self, data_dir: str, *, date_key: str):
        self.date_key = date_key
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"


@pytest.mark.asyncio
async def test_group_presence_is_separate_from_global_daily_reward() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-13")
        first = await store.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
            group_id="20001",
            group_name="Group A",
            platform="aiocqhttp",
        )
        second = await store.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
            group_id="20002",
            group_name="Group B",
            platform="aiocqhttp",
        )

        assert not first.duplicate
        assert second.duplicate
        assert (await store.get_profile("10001")).total_days == 1
        groups = await store.list_checkin_groups()
        assert {item["group_id"] for item in groups} == {"20001", "20002"}
        assert all(item["today_count"] == 1 for item in groups)


@pytest.mark.asyncio
async def test_group_rankings_and_trend_use_only_current_group() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(tmp, date_key="2026-07-12")
        await store.checkin(
            user_id="10001", username="Alice", bot_name="neko", group_id="20001"
        )
        await store.checkin(
            user_id="10002", username="Bob", bot_name="neko", group_id="20002"
        )
        store.date_key = "2026-07-13"
        await store.checkin(
            user_id="10001", username="Alice", bot_name="neko", group_id="20001"
        )
        await store.checkin(
            user_id="10002", username="Bob", bot_name="neko", group_id="20001"
        )

        month = await store.get_group_ranking(
            group_id="20001", ranking_type="month", limit=10
        )
        streak = await store.get_group_ranking(
            group_id="20001", ranking_type="streak", limit=10
        )
        trend = await store.get_group_trend(group_id="20001", days=7)

        assert [(item["user_id"], item["value"]) for item in month["entries"]] == [
            ("10001", 2),
            ("10002", 1),
        ]
        assert streak["entries"][0]["user_id"] == "10001"
        assert streak["entries"][0]["value"] == 2
        assert trend[-2:] == [
            {"date": "2026-07-12", "count": 1},
            {"date": "2026-07-13", "count": 2},
        ]


@pytest.mark.asyncio
async def test_snapshot_round_trip_preserves_group_presence() -> None:
    with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
        source = FrozenCheckinStore(src, date_key="2026-07-13")
        await source.checkin(
            user_id="10001",
            username="Alice",
            bot_name="neko",
            group_id="20001",
            group_name="Group A",
            platform="aiocqhttp",
        )
        snapshot = await source.export_snapshot()

        assert snapshot["schema_version"] == 6
        assert len(snapshot["group_presence"]) == 1

        target = FrozenCheckinStore(dst, date_key="2026-07-13")
        result = await target.import_snapshot(snapshot)
        groups = await target.list_checkin_groups()

        assert result["group_presence"] == 1
        assert groups[0]["group_name"] == "Group A"
