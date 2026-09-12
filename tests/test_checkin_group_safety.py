from __future__ import annotations

import asyncio
from contextlib import closing
import sqlite3
import tempfile
from pathlib import Path

import pytest

from checkin import CheckinStore
from checkin.schema import CHECKIN_DB_SCHEMA_VERSION
from group_safety import SessionSafetyService


class Config(dict):
    def save_config(self):
        if self.get("_fail_save"):
            raise RuntimeError("save failed")
        return None


@pytest.mark.asyncio
async def test_group_policy_defaults_are_strict_without_writing_a_row() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        policy = await SessionSafetyService(Config(content_dedupe={})).get_group_policy("group-a")
        assert policy == {
            "group_id": "group-a",
            "general_only_enabled": True,
            "builtin_terms_enabled": True,
            "updated_by": "",
            "updated_at": "",
            "is_default": True,
            "custom_terms": [],
            "blacklisted_illust_ids": [],
        }
        with closing(sqlite3.connect(store._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='group_content_safety'").fetchone() is None


@pytest.mark.asyncio
async def test_group_policy_switches_are_independent_and_persist() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        config = Config(content_dedupe={})
        service = SessionSafetyService(config)
        first = await service.upsert_group_policy(
            "group-a", general_only_enabled=False, builtin_terms_enabled=True, updated_by="test"
        )
        assert first["general_only_enabled"] is False
        assert first["builtin_terms_enabled"] is True
        reopened = SessionSafetyService(config)
        await reopened.initialize(store)
        assert await reopened.get_group_policy("group-a") == first

        reset = await reopened.upsert_group_policy("group-a", general_only_enabled=True, builtin_terms_enabled=True, updated_by="test")
        assert reset["is_default"] is False


@pytest.mark.asyncio
async def test_checkin_snapshot_import_does_not_reset_group_runtime_policy() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        snapshot = await store.export_snapshot()
        config = Config(content_dedupe={})
        service = SessionSafetyService(config)
        await service.upsert_group_policy("group-a", general_only_enabled=False, builtin_terms_enabled=False)
        await store.import_snapshot(snapshot)
        policy = await service.get_group_policy("group-a")
        assert policy["general_only_enabled"] is False
        assert policy["builtin_terms_enabled"] is False


@pytest.mark.asyncio
async def test_group_policy_validates_ids_bools_and_serializes_concurrent_writes() -> None:
    config = Config(content_dedupe={})
    service = SessionSafetyService(config)
    with pytest.raises(ValueError):
        await service.get_group_policy(" ")
    with pytest.raises(ValueError):
        await service.get_group_policy("x" * 129)
    with pytest.raises(ValueError):
        await service.upsert_group_policy(
            "group-a", general_only_enabled="false", builtin_terms_enabled=True
        )
    await asyncio.gather(*(
        service.upsert_group_policy(
            "group-a",
            general_only_enabled=bool(index % 2),
            builtin_terms_enabled=bool((index // 2) % 2),
            updated_by=str(index),
        )
        for index in range(16)
    ))
    policy = await service.get_group_policy("group-a")
    assert type(policy["general_only_enabled"]) is bool
    assert type(policy["builtin_terms_enabled"]) is bool


def test_v2_database_stays_v2_without_legacy_policy_table() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        original = CheckinStore(tmp)
        with closing(sqlite3.connect(original._db_path)) as conn:
            conn.execute("INSERT INTO checkin_global_events (event_type, date_value, name, created_by, created_at, updated_at) VALUES ('solar', '09-04', 'kept', '', 'now', 'now')")
            conn.execute("PRAGMA user_version = 2")
            conn.commit()

        migrated = CheckinStore(tmp)
        with closing(sqlite3.connect(migrated._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == CHECKIN_DB_SCHEMA_VERSION
            assert conn.execute("SELECT name FROM checkin_global_events").fetchone()[0] == "kept"
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='group_content_safety'").fetchone() is None

        backups = list((Path(tmp) / "checkin_migration_backups").glob("checkin-v2-*.sqlite3"))
        assert backups == []


@pytest.mark.parametrize("old_version", [1])
def test_known_old_versions_migrate_once_and_v3_reopen_is_idempotent(old_version) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute(f"PRAGMA user_version = {old_version}")
            conn.commit()
        CheckinStore(tmp)
        backup_dir = Path(tmp) / "checkin_migration_backups"
        before = list(backup_dir.glob("*.sqlite3"))
        assert len(before) == 1
        CheckinStore(tmp)
        assert list(backup_dir.glob("*.sqlite3")) == before


@pytest.mark.asyncio
async def test_v3_policy_table_converges_after_config_save() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute(
                "INSERT INTO checkin_users (user_id, created_at, updated_at) "
                "VALUES ('user-a', 'created', 'updated')"
            )
            conn.execute(
                "INSERT INTO checkin_records (date_key, user_id, created_at, updated_at) "
                "VALUES ('2026-09-09', 'user-a', 'created', 'updated')"
            )
            conn.execute(
                "INSERT INTO checkin_global_events "
                "(event_type, date_value, name, created_by, created_at, updated_at) "
                "VALUES ('solar', '09-09', 'kept-event', '', 'created', 'updated')"
            )
            conn.execute("CREATE TABLE group_content_safety (group_id TEXT PRIMARY KEY, general_only_enabled INTEGER NOT NULL, builtin_terms_enabled INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO group_content_safety VALUES ('group-a', 0, 1, 'legacy', 'now')")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        config = Config(content_dedupe={})
        await SessionSafetyService(config).initialize(store)
        result = store.converge_legacy_group_policy_schema()
        assert result["from_version"] == 3
        assert result["to_version"] == 2
        assert result["changed"] is True
        assert result["backup_path"]
        assert config["content_dedupe"]["group_content_safety_policies_migrated"] is True
        assert config["content_dedupe"]["group_content_safety_policies"][0]["group_id"] == "group-a"
        with closing(sqlite3.connect(store._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] in (0, 1, 2)
            assert conn.execute("SELECT user_id FROM checkin_users").fetchone()[0] == "user-a"
            assert conn.execute("SELECT user_id FROM checkin_records").fetchone()[0] == "user-a"
            assert conn.execute("SELECT name FROM checkin_global_events").fetchone()[0] == "kept-event"
            assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='group_content_safety'").fetchone()
            assert conn.execute("SELECT group_id FROM group_content_safety").fetchone()[0] == "group-a"
        backup = next((Path(tmp) / "checkin_migration_backups").glob("checkin-v3-*.sqlite3"))
        with closing(sqlite3.connect(backup)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT user_id FROM checkin_users").fetchone()[0] == "user-a"
            assert conn.execute("SELECT user_id FROM checkin_records").fetchone()[0] == "user-a"
            assert conn.execute("SELECT name FROM checkin_global_events").fetchone()[0] == "kept-event"
            assert conn.execute("SELECT group_id FROM group_content_safety").fetchone()[0] == "group-a"
        before = sorted((Path(tmp) / "checkin_migration_backups").glob("checkin-v3-*.sqlite3"))
        reopened = CheckinStore(tmp)
        assert reopened.converge_legacy_group_policy_schema()["changed"] is False
        assert sorted((Path(tmp) / "checkin_migration_backups").glob("checkin-v3-*.sqlite3")) == before
        with closing(sqlite3.connect(reopened._db_path)) as conn:
            assert conn.execute("SELECT user_id FROM checkin_users").fetchone()[0] == "user-a"
            assert conn.execute("SELECT user_id FROM checkin_records").fetchone()[0] == "user-a"
            assert conn.execute("SELECT name FROM checkin_global_events").fetchone()[0] == "kept-event"


@pytest.mark.asyncio
async def test_v3_policy_table_is_retained_when_config_save_fails() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute("CREATE TABLE group_content_safety (group_id TEXT PRIMARY KEY, general_only_enabled INTEGER NOT NULL, builtin_terms_enabled INTEGER NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO group_content_safety VALUES ('group-a', 0, 1, 'legacy', 'now')")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        config = Config(content_dedupe={})
        config["_fail_save"] = True
        await SessionSafetyService(config).initialize(store)
        with closing(sqlite3.connect(store._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT group_id FROM group_content_safety").fetchone()[0] == "group-a"


def test_v3_without_policy_table_still_backs_up_and_converges() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        reopened = CheckinStore(tmp)
        result = reopened.converge_legacy_group_policy_schema()
        assert result["backup_path"]
        with closing(sqlite3.connect(reopened._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        backup = next((Path(tmp) / "checkin_migration_backups").glob("checkin-v3-*.sqlite3"))
        with closing(sqlite3.connect(backup)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_v3_backup_failure_preserves_data_and_does_not_start_transaction(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute("CREATE TABLE group_content_safety (group_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO group_content_safety VALUES ('keep')")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        observed = []

        def fail_backup(source, version):
            observed.append(source.in_transaction)
            raise OSError("backup failed")

        monkeypatch.setattr(store, "_backup_before_migration", fail_backup)
        with pytest.raises(OSError, match="backup failed"):
            store.converge_legacy_group_policy_schema()
        assert observed == [False]
        with closing(sqlite3.connect(store._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT group_id FROM group_content_safety").fetchone()[0] == "keep"


def test_v3_transaction_failure_rolls_back_and_backup_remains_readable(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute("CREATE TABLE group_content_safety (group_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO group_content_safety VALUES ('keep')")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        original_connect = store._connect

        class FailingConnection:
            def __init__(self, connection):
                self.connection = connection

            def execute(self, sql, *args):
                if sql.startswith("PRAGMA user_version = 2"):
                    raise sqlite3.OperationalError("transaction failed")
                return self.connection.execute(sql, *args)

            def close(self):
                self.connection.close()

            def __getattr__(self, name):
                return getattr(self.connection, name)

        monkeypatch.setattr(
            store, "_connect", lambda: FailingConnection(original_connect())
        )
        with pytest.raises(sqlite3.OperationalError, match="transaction failed"):
            store.converge_legacy_group_policy_schema()
        with closing(sqlite3.connect(store._db_path)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT group_id FROM group_content_safety").fetchone()[0] == "keep"
        backup = next((Path(tmp) / "checkin_migration_backups").glob("checkin-v3-*.sqlite3"))
        with closing(sqlite3.connect(backup)) as conn:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
            assert conn.execute("SELECT group_id FROM group_content_safety").fetchone()[0] == "keep"


def test_unknown_future_schema_is_rejected_without_migration_backup() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = CheckinStore(tmp)
        with closing(sqlite3.connect(store._db_path)) as conn:
            conn.execute("PRAGMA user_version = 999")
            conn.commit()
        with pytest.raises(RuntimeError, match="unsupported"):
            CheckinStore(tmp)
        assert not (Path(tmp) / "checkin_migration_backups").exists()
