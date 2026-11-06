from __future__ import annotations

import hashlib
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin.models import CheckinProfile
from astrbot_plugin_get_px.checkin.rules import (
    CheckinRewardSettings,
    checkin_affection_bonus,
    checkin_coin_bonus,
    daily_base_reward,
    deterministic_base_coins,
    normalize_reward_settings,
)
from astrbot_plugin_get_px.main import GetPxPlugin


def legacy_draw(user_id: str, date_key: str) -> tuple[int, float]:
    seed = int.from_bytes(
        hashlib.sha256(f"checkin-reward|{date_key}|{user_id}".encode()).digest()[:8],
        "big",
    )
    rng = random.Random(seed)
    return rng.randint(50, 100), round(rng.uniform(0.50, 1.20), 2)


def expected_custom_coins(
    user_id: str, date_key: str, minimum: int, maximum: int
) -> int:
    seed = int.from_bytes(
        hashlib.sha256(f"checkin-reward|{date_key}|{user_id}".encode()).digest()[:8],
        "big",
    )
    return random.Random(seed).randint(minimum, maximum)


def preview_profile(user_id: str) -> CheckinProfile:
    return CheckinProfile(
        user_id=user_id,
        coins=0,
        affection=10,
        total_days=0,
        streak_days=0,
        last_checkin_date="",
        boost_start_date="",
        boost_until_date="",
        repeat_penalty_date="",
        repeat_penalty_total=0,
        created_at="2026-01-01T00:00:00+08:00",
        updated_at="2026-01-01T00:00:00+08:00",
    )


def test_defaults_match_exact_legacy_draws():
    for user in (f"u{i}" for i in range(31)):
        for day in ("2026-01-01", "2026-06-15", "2026-12-31"):
            assert daily_base_reward(user, day) == legacy_draw(user, day)


def test_actual_preview_custom_ranges_preserve_affection_and_coin_seed(monkeypatch):
    import astrbot_plugin_get_px.checkin.commands as command_module

    policies = [
        CheckinRewardSettings(),
        *(CheckinRewardSettings(coin_min=low, coin_max=high)
          for low, high in ((0, 0), (17, 17), (0, 1), (100, 200), (0, 10000))),
    ]
    for policy in policies:
        for user_id, day in (("u", "2026-01-01"), ("v", "2026-06-15"),
                             ("member-17", "2026-11-06")):
            monkeypatch.setattr(command_module, "daily_base_reward", daily_base_reward)
            first = GetPxPlugin._build_checkin_preview_record(
                profile=preview_profile(user_id),
                date_key=day,
                now=f"{day}T12:00:00+08:00",
                username=user_id,
                bot_name="neko",
                reward_settings=policy,
            )
            second = GetPxPlugin._build_checkin_preview_record(
                profile=preview_profile(user_id),
                date_key=day,
                now=f"{day}T12:00:00+08:00",
                username=user_id,
                bot_name="neko",
                reward_settings=policy,
            )
            assert first.base_affection == legacy_draw(user_id, day)[1]
            if policy.coin_min == 50 and policy.coin_max == 100:
                assert first.base_coins == legacy_draw(user_id, day)[0]
            else:
                expected = expected_custom_coins(
                    user_id, day, policy.coin_min, policy.coin_max
                )
                assert first.base_coins == expected
                assert first.base_coins == deterministic_base_coins(
                    user_id, day, policy.coin_min, policy.coin_max
                )
            assert first == second


def test_custom_streak_cadence_independent_caps_and_affection_rounding():
    policy = CheckinRewardSettings(
        streak_step_days=3,
        streak_coin_bonus=7,
        streak_coin_bonus_max=10,
        streak_affection_bonus=0.15,
        streak_affection_bonus_max=0.20,
    )
    bonus = checkin_coin_bonus(
        base_coins=81,
        streak_days=6,
        affection=10,
        step_percent=20,
        reward_settings=policy,
    )
    assert bonus == 28
    assert 81 + bonus == 109
    assert checkin_affection_bonus(6, reward_settings=policy) == 0.2
    assert round((0.55 + checkin_affection_bonus(6, reward_settings=policy)) * 2, 2) == 1.5


def test_invalid_native_values_default_and_reversed_pair_resets_both():
    policy, issues = normalize_reward_settings(
        {
            "coin_min": 201,
            "coin_max": 100,
            "streak_step_days": True,
            "streak_affection_bonus": float("nan"),
        }
    )
    assert (policy.coin_min, policy.coin_max) == (50, 100)
    assert policy.streak_step_days == 7 and policy.streak_affection_bonus == 0.10
    assert issues
    fixed, issues = normalize_reward_settings(
        {
            "coin_min": 0,
            "coin_max": 0,
            "streak_coin_bonus_max": 0,
            "streak_affection_bonus_max": 0,
        }
    )
    assert not issues and fixed.coin_min == fixed.coin_max == 0


def test_plugin_accessor_normalizes_native_settings_without_rewriting_config():
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {
        "checkin_basic": {
            "checkin_coin_min": 150,
            "checkin_coin_max": 40,
            "checkin_streak_step_days": True,
        }
    }
    original = dict(plugin.config["checkin_basic"])
    policy = plugin._checkin_reward_settings()
    assert (policy.coin_min, policy.coin_max, policy.streak_step_days) == (50, 100, 7)
    assert plugin.config["checkin_basic"] == original
