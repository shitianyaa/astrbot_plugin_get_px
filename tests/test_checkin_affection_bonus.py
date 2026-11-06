from __future__ import annotations

import math
import tempfile
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin.rules import checkin_coin_bonus
from astrbot_plugin_get_px.checkin import CheckinStore
import astrbot_plugin_get_px.checkin.commands as checkin_commands
import astrbot_plugin_get_px.checkin.record_store as record_store


@pytest.mark.parametrize(
    ("affection", "steps"),
    [(-10, 0), (0, 0), (9.99, 0), (10, 1), (29.99, 1), (30, 2),
     (69.99, 2), (70, 3), (139.99, 3), (140, 4), (1_000_000, 4)],
)
def test_affection_level_adds_a_coin_bonus_before_award(affection, steps):
    # 80 base + 10 streak, with 5% per level step.
    assert checkin_coin_bonus(
        base_coins=80, streak_days=7, affection=affection, step_percent=5
    ) == 10 + math.floor(90 * steps * 5 / 100)


def test_disabled_zero_and_maximum_rates_have_bounded_legacy_behavior():
    assert checkin_coin_bonus(
        base_coins=80, streak_days=7, affection=140, step_percent=25
    ) == 100
    assert checkin_coin_bonus(
        base_coins=80, streak_days=7, affection=140, step_percent=0
    ) == 10
    assert checkin_coin_bonus(
        base_coins=80, streak_days=7, affection=140, step_percent=25, enabled=False
    ) == 10
    assert checkin_coin_bonus(
        base_coins=80, streak_days=100, affection=140, step_percent=5
    ) == 76  # Streak bonus is capped at 50 before the level bonus is applied.


class FrozenCheckinStore(CheckinStore):
    date_key = "2026-05-26"

    def today_key(self):
        return self.date_key

    def now_iso(self):
        return f"{self.date_key}T12:00:00+08:00"


@pytest.mark.asyncio
async def test_preview_and_store_use_precheckin_affection_and_duplicates_keep_award(monkeypatch):
    monkeypatch.setattr(record_store, "_daily_base_reward", lambda _user, _date: (80, 0.5))
    monkeypatch.setattr(checkin_commands, "daily_base_reward", lambda _user, _date: (80, 0.5))
    with tempfile.TemporaryDirectory() as tmp:
        store = FrozenCheckinStore(Path(tmp))
        await store.get_profile("user1")
        await store.update_checkin_member(
            user_id="user1", coins=0, affection=9.9, total_days=0, streak_days=0
        )
        before = await store.get_profile("user1")
        from astrbot_plugin_get_px.main import GetPxPlugin

        preview = GetPxPlugin._build_checkin_preview_record(
            profile=before,
            date_key="2026-05-26",
            now="2026-05-26T12:00:00+08:00",
            username="user",
            bot_name="neko",
            affection_bonus_step_percent=5,
        )
        # The award uses the pre-checkin 9.9 affection even though growth crosses 10.
        assert preview.bonus_coins == 0
        assert preview.total_affection_after == 10.4
        result = await store.checkin(
            user_id="user1",
            username="user",
            bot_name="neko",
            affection_bonus_step_percent=5,
        )
        assert result.record.bonus_coins == preview.bonus_coins
        assert result.record.coins_reward == preview.coins_reward
        duplicate = await store.checkin(
            user_id="user1", username="user", bot_name="neko", affection_bonus_step_percent=25
        )
        assert duplicate.duplicate is True
        assert duplicate.record == result.record

        store.date_key = "2026-05-27"
        next_result = await store.checkin(
            user_id="user1", username="user", bot_name="neko", affection_bonus_step_percent=5
        )
        assert next_result.record.bonus_coins == 4
