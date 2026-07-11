from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from checkin import ACHIEVEMENTS, CheckinStore
from checkin_birthday import birthday_matches, parse_month_day, parse_qq_birthday


@pytest.mark.asyncio
async def test_birthday_manual_priority_clear_and_qq_checked_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        initial = await store.get_user_preference("10001")
        assert not initial.qq_birthday_checked
        manual = await store.set_birthday(
            user_id="10001", month=7, day=11, source="manual"
        )
        assert manual.birthday_label == "07-11"
        assert manual.birthday_source == "manual"
        cleared = await store.clear_birthday("10001")
        assert cleared.birthday_label == ""
        assert not cleared.qq_birthday_checked
        await store.mark_qq_birthday_checked("10001")
        assert (await store.get_user_preference("10001")).qq_birthday_checked


def test_qq_birthday_parser_accepts_common_shapes_and_leap_day() -> None:
    assert parse_qq_birthday({"birthday_month": 2, "birthday_day": 29}) == (2, 29)
    assert parse_qq_birthday({"birthday": "2000-07-11"}) == (7, 11)
    assert parse_qq_birthday({"birthday": {"month": "10", "day": "1"}}) == (10, 1)
    assert parse_month_day("07-11") == (7, 11)
    assert parse_month_day(20000711) == (7, 11)
    assert birthday_matches("2028-02-29", 2, 29)
    assert not birthday_matches("2027-02-28", 2, 29)


@pytest.mark.asyncio
async def test_global_events_support_once_annual_priority_and_uniqueness() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        annual = await store.add_global_event(
            event_type="annual", date_value="07-11", name="相遇纪念日", created_by="1"
        )
        once = await store.add_global_event(
            event_type="once", date_value="2026-07-11", name="特别活动", created_by="1"
        )
        events = await store.events_for_date("2026-07-11")
        assert [item.event_id for item in events] == [once.event_id, annual.event_id]
        with pytest.raises(ValueError, match="已存在"):
            await store.add_global_event(
                event_type="annual", date_value="07-11", name="重复", created_by="1"
            )
        assert await store.delete_global_event(once.event_id)


@pytest.mark.asyncio
async def test_achievements_unlock_idempotently_and_titles_require_unlock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        profile = await store.get_profile("10001")
        profile = replace(profile, total_days=30, streak_days=7)
        unlocked = await store.unlock_achievements(profile)
        assert unlocked == ("first_meeting", "streak_7", "total_30")
        assert await store.unlock_achievements(profile) == ()
        preference = await store.get_user_preference("10001")
        assert preference.selected_title_id == "first_meeting"
        assert await store.select_title(user_id="10001", title="七日同行") == "streak_7"
        with pytest.raises(ValueError, match="尚未解锁"):
            await store.select_title(user_id="10001", title="千日物语")


@pytest.mark.asyncio
async def test_v3_snapshot_preserves_feature_tables() -> None:
    with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
        source = CheckinStore(source_tmp)
        await source.get_profile("10001")
        await source.set_birthday(user_id="10001", month=7, day=11, source="manual")
        profile = replace(await source.get_profile("10001"), total_days=1)
        await source.unlock_achievements(profile)
        await source.add_global_event(
            event_type="annual", date_value="07-11", name="相遇纪念日", created_by="1"
        )
        snapshot = await source.export_snapshot()
        assert snapshot["schema_version"] == 3

        target = CheckinStore(target_tmp)
        summary = await target.import_snapshot(snapshot)
        assert summary["preferences"] == 1
        assert summary["global_events"] == 1
        assert summary["achievements"] == 1
        assert (await target.get_user_preference("10001")).birthday_label == "07-11"
        assert (await target.list_achievements("10001")) == ("first_meeting",)
        assert len(await target.list_global_events()) == 1


@pytest.mark.asyncio
async def test_birthday_can_be_backed_up_before_first_checkin() -> None:
    with tempfile.TemporaryDirectory() as source_tmp, tempfile.TemporaryDirectory() as target_tmp:
        source = CheckinStore(source_tmp)
        await source.set_birthday(user_id="new-user", month=2, day=29, source="manual")
        snapshot = await source.export_snapshot()
        assert snapshot["profiles"] == []
        target = CheckinStore(target_tmp)
        await target.import_snapshot(snapshot)
        assert (await target.get_user_preference("new-user")).birthday_label == "02-29"


@pytest.mark.asyncio
async def test_v3_snapshot_requires_feature_arrays_and_rejects_bool_version() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        snapshot = await store.export_snapshot()
        missing = dict(snapshot)
        missing.pop("preferences")
        with pytest.raises(ValueError, match="preferences"):
            await store.import_snapshot(missing)
        invalid = dict(snapshot)
        invalid["schema_version"] = True
        with pytest.raises(ValueError, match="不支持"):
            await store.import_snapshot(invalid)


@pytest.mark.asyncio
async def test_qq_birthday_never_overwrites_manual_value() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        await store.set_birthday(user_id="10001", month=7, day=11, source="manual")
        result = await store.set_qq_birthday_if_not_manual(
            user_id="10001", month=8, day=8
        )
        assert result.birthday_label == "07-11"
        assert result.birthday_source == "manual"


@pytest.mark.asyncio
async def test_global_event_name_removes_control_characters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        event = await store.add_global_event(
            event_type="annual",
            date_value="07-11",
            name="相遇\x00\x07 纪念日",
            created_by="1",
        )
        assert event.name == "相遇 纪念日"
