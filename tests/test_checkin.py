import tempfile
import unittest
from pathlib import Path

from checkin import (
    BOOST_PRODUCTS,
    CheckinStore,
    affection_level,
    boost_remaining_days,
    is_boost_active,
)
from checkin_background import (
    filter_illusts_by_aspect_ratio,
    illust_aspect_ratio,
    parse_aspect_ratio,
)


class FrozenCheckinStore(CheckinStore):
    def __init__(self, data_dir: Path | str, *, date_key: str = "2026-05-26"):
        self.date_key = date_key
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"


class CheckinStoreTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_checkin_rewards_and_records_global_user_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)

            result = await store.checkin(
                user_id="10001",
                username="tester",
                bot_name="neko",
            )

            self.assertFalse(result.duplicate)
            self.assertIsNotNone(result.record)
            self.assertGreaterEqual(result.record.coins_reward, 50)
            self.assertLessEqual(result.record.coins_reward, 100)
            self.assertGreaterEqual(result.record.affection_reward, 0.50)
            self.assertLessEqual(result.record.affection_reward, 1.20)
            self.assertEqual(result.profile.total_days, 1)
            self.assertEqual(result.profile.streak_days, 1)
            self.assertEqual(result.profile.last_checkin_date, "2026-05-26")

    async def test_duplicate_checkin_penalizes_affection_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            first = await store.checkin(
                user_id="10001",
                username="tester",
                bot_name="neko",
            )

            duplicate = await store.checkin(
                user_id="10001",
                username="tester",
                bot_name="neko",
            )

            self.assertTrue(duplicate.duplicate)
            self.assertEqual(duplicate.penalty_amount, 0.20)
            self.assertEqual(duplicate.profile.total_days, first.profile.total_days)
            self.assertEqual(duplicate.profile.streak_days, first.profile.streak_days)
            self.assertEqual(duplicate.profile.coins, first.profile.coins)
            self.assertAlmostEqual(
                duplicate.profile.affection,
                first.profile.affection - 0.20,
                places=2,
            )
            self.assertEqual(
                duplicate.record.affection_reward,
                first.record.affection_reward,
            )

    async def test_duplicate_penalty_has_daily_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            await store.checkin(user_id="10001", username="tester", bot_name="neko")

            last = None
            for _ in range(10):
                last = await store.checkin(
                    user_id="10001",
                    username="tester",
                    bot_name="neko",
                )

            self.assertIsNotNone(last)
            self.assertEqual(last.penalty_total_today, 1.00)
            self.assertEqual(last.penalty_amount, 0.00)

    async def test_streak_continues_by_beijing_date_and_resets_after_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            day1 = await store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )
            store.date_key = "2026-05-27"
            day2 = await store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )
            store.date_key = "2026-05-29"
            day4 = await store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )

            self.assertEqual(day1.profile.streak_days, 1)
            self.assertEqual(day2.profile.streak_days, 2)
            self.assertEqual(day4.profile.streak_days, 1)
            self.assertEqual(day4.profile.total_days, 3)

    async def test_buy_boost_after_checkin_starts_tomorrow(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            for day in range(26, 30):
                store.date_key = f"2026-05-{day}"
                await store.checkin(
                    user_id="10001",
                    username="tester",
                    bot_name="neko",
                )
            profile = await store.get_profile("10001")
            self.assertGreaterEqual(profile.coins, BOOST_PRODUCTS[1])

            bought = await store.purchase_boost(user_id="10001", days=1)

            self.assertTrue(bought.success)
            self.assertFalse(is_boost_active(bought.profile, "2026-05-29"))
            self.assertTrue(is_boost_active(bought.profile, "2026-05-30"))
            self.assertEqual(boost_remaining_days(bought.profile, "2026-05-30"), 1)

    async def test_affection_level_thresholds(self):
        self.assertEqual(affection_level(-0.01)["name"], "排斥")
        self.assertEqual(affection_level(0)["name"], "陌生")
        self.assertEqual(affection_level(10)["name"], "熟悉")
        self.assertEqual(affection_level(30)["name"], "亲近")
        self.assertEqual(affection_level(70)["name"], "信赖")
        self.assertEqual(affection_level(140)["name"], "挚友")


class CheckinBackgroundTest(unittest.TestCase):
    def test_parse_aspect_ratio_accepts_common_formats(self):
        self.assertAlmostEqual(parse_aspect_ratio("2.2:1"), 2.2)
        self.assertAlmostEqual(parse_aspect_ratio("16/9"), 16 / 9)
        self.assertAlmostEqual(parse_aspect_ratio("3 x 4"), 0.75)
        self.assertAlmostEqual(parse_aspect_ratio("1.777"), 1.777)

    def test_parse_aspect_ratio_rejects_invalid_values(self):
        self.assertEqual(parse_aspect_ratio(""), 0.0)
        self.assertEqual(parse_aspect_ratio("abc"), 0.0)
        self.assertEqual(parse_aspect_ratio("16:0"), 0.0)
        self.assertEqual(parse_aspect_ratio("-1"), 0.0)

    def test_filter_illusts_by_aspect_ratio_uses_top_level_dimensions(self):
        illusts = [
            {"id": "wide", "width": 2200, "height": 1000},
            {"id": "square", "width": 1000, "height": 1000},
            {"id": "portrait", "width": 900, "height": 1200},
        ]

        filtered = filter_illusts_by_aspect_ratio(illusts, 2.2, 0.05)

        self.assertEqual([illust["id"] for illust in filtered], ["wide"])

    def test_filter_illusts_by_aspect_ratio_respects_tolerance(self):
        illusts = [
            {"id": "near", "width": 1920, "height": 1080},
            {"id": "far", "width": 1000, "height": 1000},
        ]

        strict = filter_illusts_by_aspect_ratio(illusts, 16 / 9, 0.0)
        relaxed = filter_illusts_by_aspect_ratio(illusts, 2.0, 0.15)

        self.assertEqual([illust["id"] for illust in strict], ["near"])
        self.assertEqual([illust["id"] for illust in relaxed], ["near"])

    def test_unknown_dimensions_do_not_match_limited_ratio(self):
        self.assertEqual(illust_aspect_ratio({"id": "unknown"}), 0.0)
        self.assertEqual(
            filter_illusts_by_aspect_ratio([{"id": "unknown"}], 2.2, 0.15),
            [],
        )


if __name__ == "__main__":
    unittest.main()
