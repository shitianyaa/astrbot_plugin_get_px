import asyncio
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from checkin import (
    BOOST_PRODUCTS,
    CheckinStore,
    affection_level,
    boost_remaining_days,
    dump_checkin_snapshot_json,
    is_boost_active,
    load_checkin_snapshot_json,
)
from checkin.background import (
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


class RacingCheckinStore(FrozenCheckinStore):
    def __init__(self, data_dir: Path | str, *, date_key: str = "2026-05-26"):
        self.content_barrier: threading.Barrier | None = None
        super().__init__(data_dir, date_key=date_key)

    def _row_to_record(self, row):
        record = CheckinStore._row_to_record(row)
        if (
            self.content_barrier is not None
            and record.greeting_source == "local"
            and record.greeting == "Local greeting"
        ):
            self.content_barrier.wait(timeout=5)
        return record


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

    async def test_duplicate_checkin_returns_existing_record_without_penalty(self):
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
            self.assertEqual(duplicate.record, first.record)
            self.assertEqual(duplicate.profile, first.profile)
            self.assertEqual(duplicate.penalty_amount, 0)
            self.assertEqual(duplicate.penalty_total_today, 0)

    async def test_repeated_duplicate_checkins_never_change_affection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            first = await store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )

            last = None
            for _ in range(10):
                last = await store.checkin(
                    user_id="10001",
                    username="tester",
                    bot_name="neko",
                )

            self.assertIsNotNone(last)
            self.assertEqual(last.profile, first.profile)
            self.assertEqual(last.record, first.record)
            self.assertEqual(last.penalty_total_today, 0)
            self.assertEqual(last.penalty_amount, 0)

    async def test_legacy_database_adds_card_content_columns_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "checkin.sqlite3"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE checkin_records (
                        date_key TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        username TEXT NOT NULL DEFAULT '',
                        bot_name TEXT NOT NULL DEFAULT '',
                        base_coins INTEGER NOT NULL DEFAULT 0,
                        bonus_coins INTEGER NOT NULL DEFAULT 0,
                        coins_reward INTEGER NOT NULL DEFAULT 0,
                        base_affection REAL NOT NULL DEFAULT 0,
                        bonus_affection REAL NOT NULL DEFAULT 0,
                        affection_reward REAL NOT NULL DEFAULT 0,
                        boost_active INTEGER NOT NULL DEFAULT 0,
                        boost_multiplier REAL NOT NULL DEFAULT 1,
                        total_coins_after INTEGER NOT NULL DEFAULT 0,
                        total_affection_after REAL NOT NULL DEFAULT 0,
                        total_days_after INTEGER NOT NULL DEFAULT 0,
                        streak_days_after INTEGER NOT NULL DEFAULT 0,
                        note TEXT NOT NULL DEFAULT '',
                        background_mode TEXT NOT NULL DEFAULT '',
                        background_source TEXT NOT NULL DEFAULT '',
                        background_illust_id TEXT NOT NULL DEFAULT '',
                        background_title TEXT NOT NULL DEFAULT '',
                        background_author TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (date_key, user_id)
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO checkin_records (
                        date_key, user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        "2026-05-26",
                        "10001",
                        "2026-05-26T12:00:00+08:00",
                        "2026-05-26T12:00:00+08:00",
                    ),
                )
                conn.commit()

            store = FrozenCheckinStore(tmp)

            with closing(sqlite3.connect(db_path)) as conn:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(checkin_records)")
                }
            self.assertTrue(
                {
                    "event_key",
                    "event_label",
                    "greeting",
                    "greeting_source",
                    "secondary_note",
                    "template_version",
                }.issubset(columns)
            )

            record = await store.get_today_record("10001")
            self.assertIsNotNone(record)
            self.assertEqual(record.event_key, "")
            self.assertEqual(record.event_label, "")
            self.assertEqual(record.greeting, "")
            self.assertEqual(record.greeting_source, "local")
            self.assertEqual(record.secondary_note, "")
            self.assertEqual(record.template_version, "v2")

    async def test_record_content_persists_once_and_only_local_can_upgrade_to_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            checked = await store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )
            self.assertEqual(checked.record.greeting_source, "local")
            self.assertEqual(checked.record.template_version, "v2")

            local = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="summer",
                event_label="Summer Day",
                greeting="Local greeting",
                greeting_source="local",
                secondary_note="Local note",
                template_version="v2",
            )
            ignored_local = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="other",
                event_label="Other",
                greeting="Other local greeting",
                greeting_source="local",
                secondary_note="Other note",
                template_version="v3",
            )
            self.assertEqual(ignored_local, local)

            ai = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="summer",
                event_label="Summer Day",
                greeting="AI greeting",
                greeting_source="ai",
                secondary_note="AI note",
                template_version="v2",
            )
            self.assertEqual(ai.greeting, "AI greeting")
            self.assertEqual(ai.greeting_source, "ai")
            self.assertEqual(ai.secondary_note, "AI note")

            ignored_ai = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="changed",
                event_label="Changed",
                greeting="Second AI greeting",
                greeting_source="ai",
                secondary_note="Changed note",
                template_version="v3",
            )
            self.assertEqual(ignored_ai, ai)

    async def test_competing_ai_upgrades_write_once_and_return_winning_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_store = RacingCheckinStore(tmp)
            second_store = RacingCheckinStore(tmp)
            await first_store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )
            await first_store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="summer",
                event_label="Summer Day",
                greeting="Local greeting",
                greeting_source="local",
                secondary_note="Local note",
                template_version="v2",
            )
            with closing(sqlite3.connect(Path(tmp) / "checkin.sqlite3")) as conn:
                conn.execute(
                    "CREATE TABLE content_update_audit (greeting TEXT NOT NULL)"
                )
                conn.execute(
                    """
                    CREATE TRIGGER audit_ai_content_update
                    AFTER UPDATE OF greeting_source ON checkin_records
                    WHEN NEW.greeting_source = 'ai'
                    BEGIN
                        INSERT INTO content_update_audit (greeting)
                        VALUES (NEW.greeting);
                    END
                    """
                )
                conn.commit()

            barrier = threading.Barrier(2)
            first_store.content_barrier = barrier
            second_store.content_barrier = barrier
            first_result, second_result = await asyncio.gather(
                first_store.update_record_content(
                    user_id="10001",
                    date_key="2026-05-26",
                    event_key="summer",
                    event_label="Summer Day",
                    greeting="First AI greeting",
                    greeting_source="ai",
                    secondary_note="First AI note",
                    template_version="v2",
                ),
                second_store.update_record_content(
                    user_id="10001",
                    date_key="2026-05-26",
                    event_key="summer",
                    event_label="Summer Day",
                    greeting="Second AI greeting",
                    greeting_source="ai",
                    secondary_note="Second AI note",
                    template_version="v2",
                ),
            )
            first_store.content_barrier = None
            second_store.content_barrier = None

            final_record = await first_store.get_today_record("10001")
            with closing(sqlite3.connect(Path(tmp) / "checkin.sqlite3")) as conn:
                audit_count = conn.execute(
                    "SELECT COUNT(*) FROM content_update_audit"
                ).fetchone()[0]

            self.assertEqual(audit_count, 1)
            self.assertEqual(first_result, final_record)
            self.assertEqual(second_result, final_record)
            self.assertIn(
                final_record.greeting,
                {"First AI greeting", "Second AI greeting"},
            )

    async def test_record_content_rejects_invalid_greeting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            checked = await store.checkin(
                user_id="10001", username="tester", bot_name="neko"
            )

            with self.assertRaisesRegex(ValueError, "greeting_source"):
                await store.update_record_content(
                    user_id="10001",
                    date_key="2026-05-26",
                    event_key="summer",
                    event_label="Summer Day",
                    greeting="Remote greeting",
                    greeting_source="remote",
                    secondary_note="Remote note",
                    template_version="v2",
                )

            self.assertEqual(await store.get_today_record("10001"), checked.record)

    async def test_hitokoto_can_upgrade_local_content_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            await store.checkin(user_id="10001", username="tester", bot_name="neko")
            await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="normal",
                event_label="",
                greeting="Local greeting",
                greeting_source="local",
                secondary_note="",
                template_version="v2",
            )

            remote = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="normal",
                event_label="",
                greeting="Hitokoto greeting",
                greeting_source="hitokoto",
                greeting_attribution="毛不易 · 芬芳一生",
                secondary_note="",
                template_version="v2",
            )

            self.assertEqual(remote.greeting_source, "hitokoto")
            self.assertEqual(remote.greeting, "Hitokoto greeting")
            self.assertEqual(remote.greeting_attribution, "毛不易 · 芬芳一生")

    async def test_empty_ai_update_cannot_reopen_local_content_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            await store.checkin(user_id="10001", username="tester", bot_name="neko")
            local = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="summer",
                event_label="Summer Day",
                greeting="Local greeting",
                greeting_source="local",
                secondary_note="Local note",
                template_version="v2",
            )

            empty_ai = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="",
                event_label="",
                greeting="",
                greeting_source="ai",
                secondary_note="",
                template_version="v2",
            )
            later_local = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="changed",
                event_label="Changed",
                greeting="Changed local greeting",
                greeting_source="local",
                secondary_note="Changed local note",
                template_version="v2",
            )

            self.assertEqual(empty_ai, local)
            self.assertEqual(later_local, local)

    async def test_empty_ai_record_cannot_transition_back_to_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp)
            await store.checkin(user_id="10001", username="tester", bot_name="neko")
            with closing(sqlite3.connect(Path(tmp) / "checkin.sqlite3")) as conn:
                conn.execute(
                    """
                    UPDATE checkin_records
                    SET greeting_source = 'ai'
                    WHERE user_id = ? AND date_key = ?
                    """,
                    ("10001", "2026-05-26"),
                )
                conn.commit()

            result = await store.update_record_content(
                user_id="10001",
                date_key="2026-05-26",
                event_key="summer",
                event_label="Summer Day",
                greeting="Local greeting",
                greeting_source="local",
                secondary_note="Local note",
                template_version="v2",
            )

            self.assertEqual(result.greeting_source, "ai")
            self.assertEqual(result.greeting, "")

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

    async def test_export_import_round_trip_preserves_profile_and_records(self):
        with (
            tempfile.TemporaryDirectory() as src_tmp,
            tempfile.TemporaryDirectory() as dst_tmp,
        ):
            source = FrozenCheckinStore(src_tmp, date_key="2026-05-26")
            for day in range(26, 30):
                source.date_key = f"2026-05-{day}"
                await source.checkin(
                    user_id="10001",
                    username="tester",
                    bot_name="neko",
                )
            purchase = await source.purchase_boost(user_id="10001", days=1)
            self.assertTrue(purchase.success)
            await source.update_record_background(
                user_id="10001",
                date_key="2026-05-29",
                mode="pixiv_daily",
                source="search:blue_archive",
                illust_id="445566",
                title="Blue Sky",
                author="Someone",
            )
            await source.update_record_content(
                user_id="10001",
                date_key="2026-05-29",
                event_key="summer",
                event_label="Summer Day",
                greeting="Local welcome",
                greeting_source="local",
                secondary_note="Local note",
                template_version="v2",
            )
            await source.update_record_content(
                user_id="10001",
                date_key="2026-05-29",
                event_key="summer",
                event_label="Summer Day",
                greeting="Welcome back",
                greeting_source="ai",
                secondary_note="Stay hydrated",
                template_version="v2",
            )

            snapshot = await source.export_snapshot()
            self.assertEqual(snapshot["schema_version"], 5)
            serialized = dump_checkin_snapshot_json(snapshot)
            restored = load_checkin_snapshot_json(serialized.encode("utf-8"))

            target = FrozenCheckinStore(dst_tmp, date_key="2026-05-29")
            summary = await target.import_snapshot(restored)
            profile = await target.get_profile("10001")
            record = await target.get_today_record("10001")

            self.assertEqual(summary["profiles"], 1)
            self.assertEqual(summary["records"], 4)
            self.assertEqual(profile.total_days, 4)
            self.assertEqual(profile.last_checkin_date, "2026-05-29")
            self.assertEqual(profile.boost_start_date, "2026-05-30")
            self.assertEqual(profile.boost_until_date, "2026-05-30")
            self.assertIsNotNone(record)
            self.assertEqual(record.background_mode, "pixiv_daily")
            self.assertEqual(record.background_source, "search:blue_archive")
            self.assertEqual(record.background_illust_id, "445566")
            self.assertEqual(record.background_title, "Blue Sky")
            self.assertEqual(record.background_author, "Someone")
            self.assertEqual(record.event_key, "summer")
            self.assertEqual(record.event_label, "Summer Day")
            self.assertEqual(record.greeting, "Welcome back")
            self.assertEqual(record.greeting_source, "ai")
            self.assertEqual(record.secondary_note, "Stay hydrated")
            self.assertEqual(record.template_version, "v2")

    async def test_version_one_snapshot_imports_with_v2_record_defaults(self):
        with (
            tempfile.TemporaryDirectory() as src_tmp,
            tempfile.TemporaryDirectory() as dst_tmp,
        ):
            source = FrozenCheckinStore(src_tmp, date_key="2026-05-26")
            await source.checkin(user_id="20002", username="source", bot_name="neko")
            legacy = await source.export_snapshot()
            legacy["schema_version"] = 1
            for key in (
                "event_key",
                "event_label",
                "greeting",
                "greeting_source",
                "greeting_attribution",
                "secondary_note",
                "template_version",
            ):
                legacy["records"][0].pop(key, None)

            normalized = load_checkin_snapshot_json(
                dump_checkin_snapshot_json(legacy).encode("utf-8")
            )
            self.assertEqual(normalized["schema_version"], 5)
            self.assertEqual(normalized["records"][0]["event_key"], "")
            self.assertEqual(normalized["records"][0]["event_label"], "")
            self.assertEqual(normalized["records"][0]["greeting"], "")
            self.assertEqual(normalized["records"][0]["greeting_source"], "local")
            self.assertEqual(normalized["records"][0]["greeting_attribution"], "")
            self.assertEqual(normalized["records"][0]["secondary_note"], "")
            self.assertEqual(normalized["records"][0]["template_version"], "v2")

            target = FrozenCheckinStore(dst_tmp, date_key="2026-05-26")
            summary = await target.import_snapshot(normalized)
            record = await target.get_today_record("20002")
            self.assertEqual(summary["schema_version"], 5)
            self.assertIsNotNone(record)
            self.assertEqual(record.greeting_source, "local")
            self.assertEqual(record.greeting_attribution, "")
            self.assertEqual(record.template_version, "v2")

    async def test_version_two_snapshot_rejects_invalid_greeting_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            await store.checkin(user_id="20002", username="source", bot_name="neko")
            snapshot = await store.export_snapshot()
            snapshot["records"][0]["greeting_source"] = "remote"

            with self.assertRaisesRegex(ValueError, "greeting_source"):
                load_checkin_snapshot_json(
                    dump_checkin_snapshot_json(snapshot).encode("utf-8")
                )

    async def test_import_overwrites_existing_data(self):
        with (
            tempfile.TemporaryDirectory() as src_tmp,
            tempfile.TemporaryDirectory() as dst_tmp,
        ):
            source = FrozenCheckinStore(src_tmp, date_key="2026-05-26")
            await source.checkin(user_id="20002", username="source", bot_name="neko")
            snapshot = await source.export_snapshot()

            target = FrozenCheckinStore(dst_tmp, date_key="2026-05-26")
            await target.checkin(user_id="10001", username="target", bot_name="neko")
            await target.import_snapshot(snapshot)

            old_profile = await target.get_profile("10001")
            new_profile = await target.get_profile("20002")
            self.assertEqual(old_profile.total_days, 0)
            self.assertEqual(new_profile.total_days, 1)
            self.assertIsNotNone(await target.get_today_record("20002"))
            self.assertIsNone(await target.get_today_record("10001"))

    async def test_import_rejects_invalid_snapshot_without_mutating_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            await store.checkin(user_id="10001", username="tester", bot_name="neko")
            before = await store.export_snapshot()

            with self.assertRaisesRegex(ValueError, "不支持的签到备份版本"):
                await store.import_snapshot(
                    {
                        **before,
                        "schema_version": 999,
                    }
                )

            after = await store.export_snapshot()
            self.assertEqual(after, before)

    async def test_import_rolls_back_when_rows_conflict(self):
        with (
            tempfile.TemporaryDirectory() as src_tmp,
            tempfile.TemporaryDirectory() as dst_tmp,
        ):
            source = FrozenCheckinStore(src_tmp, date_key="2026-05-26")
            await source.checkin(user_id="20002", username="source", bot_name="neko")
            snapshot = await source.export_snapshot()
            snapshot["records"].append(dict(snapshot["records"][0]))

            target = FrozenCheckinStore(dst_tmp, date_key="2026-05-26")
            await target.checkin(user_id="10001", username="target", bot_name="neko")
            before = await target.export_snapshot()

            with self.assertRaises(ValueError):
                await target.import_snapshot(snapshot)

            after = await target.export_snapshot()
            self.assertEqual(after, before)

    async def test_import_rejects_orphan_record_without_mutating_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            await store.checkin(user_id="10001", username="target", bot_name="neko")
            before = await store.export_snapshot()
            invalid = {**before, "profiles": []}

            with self.assertRaisesRegex(ValueError, "records\\[0\\].*user_id"):
                await store.import_snapshot(invalid)

            self.assertEqual(await store.export_snapshot(), before)

    async def test_import_rejects_duplicate_profile_user_id_without_mutating_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            await store.checkin(user_id="10001", username="target", bot_name="neko")
            before = await store.export_snapshot()
            invalid = {
                **before,
                "profiles": [*before["profiles"], dict(before["profiles"][0])],
            }

            with self.assertRaisesRegex(ValueError, "profiles\\[1\\].*user_id"):
                await store.import_snapshot(invalid)

            self.assertEqual(await store.export_snapshot(), before)

    async def test_import_rejects_duplicate_record_key_without_mutating_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            await store.checkin(user_id="10001", username="target", bot_name="neko")
            before = await store.export_snapshot()
            invalid = {
                **before,
                "records": [*before["records"], dict(before["records"][0])],
            }

            with self.assertRaisesRegex(
                ValueError, "records\\[1\\].*date_key.*user_id"
            ):
                await store.import_snapshot(invalid)

            self.assertEqual(await store.export_snapshot(), before)


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
