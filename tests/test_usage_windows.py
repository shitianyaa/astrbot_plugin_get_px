"""固定去重窗口、旧记录兼容、跨来源身份和未知送达保护。"""

import asyncio
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore


def freeze(store, value):
    now = datetime.fromisoformat(value)
    store.today_key = lambda: now.date().isoformat()
    store.now_iso = lambda: now.isoformat(timespec="seconds")


@pytest.fixture
def store(tmp_path):
    index = ImageIndexStore(tmp_path)
    freeze(index, "2026-11-15T23:55:00+08:00")
    yield index
    index.close()


async def record(store, feature="normal", identity="42", source="pixiv:search:a", **kwargs):
    await store.record_usage(scope="group:1", source_key=source, illust_id=identity, feature=feature, **kwargs)


async def claim(store, feature="normal_pending", identity="42", source="lolicon:random", **kwargs):
    return await store.claim_usage(scope="group:1", source_key=source, illust_id=identity, feature=feature, **kwargs)


@pytest.mark.asyncio
async def test_ordinary_window_is_rolling_across_midnight_and_shared_across_sources(store):
    await record(store, identity="42:0")
    freeze(store, "2026-11-16T00:04:59+08:00")
    assert await store.get_used_illust_ids("group:1", "pixiv:ranking:other") == {"42"}
    assert not await claim(store, identity="42")
    freeze(store, "2026-11-16T00:05:00+08:00")
    assert await store.get_used_illust_ids("group:1", "lolicon:random") == set()
    assert await claim(store)


@pytest.mark.asyncio
async def test_checkin_uses_two_beijing_dates_and_has_independent_history(store):
    await record(store, feature="checkin")
    assert await store.get_used_illust_ids("group:1", "pixiv:search:a") == set()
    assert await claim(store)
    freeze(store, "2026-11-16T23:59:59+08:00")
    assert await store.get_used_illust_ids("group:1", "other", feature="checkin") == {"42"}
    assert not await claim(store, feature="checkin_pending")
    freeze(store, "2026-11-17T00:00:00+08:00")
    assert not await store.get_used_illust_ids("group:1", "other", feature="checkin")
    assert await claim(store, feature="checkin_pending")


@pytest.mark.asyncio
async def test_ordinary_history_does_not_block_checkin_or_other_sessions(store):
    await record(store)
    assert await claim(store, feature="checkin_pending")
    assert await store.claim_usage(scope="group:2", source_key="pixiv:search:a", illust_id="42", feature="normal_pending")
    assert not await claim(store, source="different-tag")


@pytest.mark.asyncio
@pytest.mark.parametrize("with_completed_record", [False, True])
async def test_ordinary_pending_expires_at_ten_minutes_even_for_explicit_retry(store, with_completed_record):
    assert await claim(store)
    if with_completed_record:
        await record(store, keep_pending=True)
    freeze(store, "2026-11-16T00:04:59+08:00")
    assert not await claim(store, ignore_history=True)
    assert await store.get_used_illust_ids("group:1", "any", ignore_history=True) == {"42"}
    freeze(store, "2026-11-16T00:05:00+08:00")
    assert not await store.get_used_illust_ids("group:1", "any", ignore_history=True)
    assert await claim(store, ignore_history=True)


@pytest.mark.asyncio
async def test_explicit_history_exception_does_not_ignore_another_page_pending(store):
    await record(store)
    assert await claim(store, ignore_history=True)
    assert not await claim(store, ignore_history=True, identity="42:0")
    assert await claim(store, identity="42:1")


@pytest.mark.asyncio
async def test_post_reset_records_with_legacy_source_keys_still_dedupe(store):
    conn = store._get_conn()
    conn.execute("INSERT INTO image_usage VALUES (?, ?, ?, ?, ?, ?, ?)", (
        store.today_key(), "group:1", "pixiv:search:old-hash", "42:0", "normal", "user", store.now_iso(),
    ))
    conn.commit()
    assert not await claim(store, identity="42")
    assert await store.get_used_illust_ids("group:1", "lolicon:random") == {"42"}


@pytest.mark.asyncio
async def test_concurrent_sources_and_connections_claim_the_same_identity_once(store, tmp_path):
    other = ImageIndexStore(tmp_path)
    freeze(other, store.now_iso())
    try:
        results = await asyncio.gather(claim(store), claim(other, source="pixiv:ranking:day", identity="42:0"))
        assert sorted(results) == [False, True]
    finally:
        other.close()


@pytest.mark.asyncio
async def test_cleanup_preserves_checkin_while_expiring_normal_history_and_pending(store):
    await record(store, identity="1")
    await record(store, feature="checkin", identity="2")
    assert await claim(store, identity="3")
    assert await claim(store, feature="checkin_pending", identity="4")
    freeze(store, "2026-11-16T00:10:00+08:00")
    await store.cleanup_old_days()
    assert await store.get_used_illust_ids("group:1", "any") == set()
    assert await store.get_used_illust_ids("group:1", "any", feature="checkin") == {"2", "4"}


@pytest.mark.asyncio
async def test_cursor_business_isolation_and_rolling_day_expiry(store):
    scope, key = "group:1", "pixiv:search:洛茜 黑丝"
    conn = store._get_conn()
    conn.execute("INSERT INTO source_page_cursor VALUES (?, ?, ?, ?)", (scope, key, 90, store.now_iso()))
    conn.commit()
    assert await store.get_page_offset(scope, key) == 0
    assert await store.get_page_offset(scope, key, feature="checkin") == 0
    await record(store)
    await record(store, feature="checkin")
    await store.advance_page_offset(scope, key, 30)
    await store.advance_page_offset(scope, key, 60, feature="checkin")
    freeze(store, "2026-11-16T00:05:00+08:00")
    assert not await store.get_used_illust_ids(scope, "any")
    assert await store.get_used_illust_ids(scope, "any", feature="checkin") == {"42"}
    for feature, expected in (("normal", 30), ("checkin", 60)):
        assert await store.get_page_offset(scope, key, feature=feature) == expected
    freeze(store, "2026-11-16T23:54:59+08:00")
    for feature, expected in (("normal", 30), ("checkin", 60)):
        assert await store.get_page_offset(scope, key, feature=feature) == expected
    freeze(store, "2026-11-16T23:55:00+08:00")
    assert await store.get_page_offset(scope, key) == 0
    # 推进也必须忽略过期起点；签到与普通分页共用24小时期限但不共享位置。
    assert await store.advance_page_offset(scope, key, 30) == 30
    assert await store.get_page_offset(scope, key, feature="checkin") == 0
    await store.advance_page_offset(scope, key, 60, feature="checkin")
    await store.reset_page_offset(scope, key)
    assert await store.get_page_offset(scope, key) == 0
    assert await store.get_page_offset(scope, key, feature="checkin") == 60


@pytest.mark.asyncio
async def test_cleanup_removes_idle_expired_cursors_without_shortening_usage_windows(store):
    freeze(store, "2026-11-16T11:50:00+08:00")
    await record(store, identity="1")
    await record(store, feature="checkin", identity="2")
    assert await claim(store, identity="3")
    assert await claim(store, feature="checkin_pending", identity="4")
    freeze(store, "2026-11-16T11:59:59+08:00")
    await record(store, identity="5")
    assert await claim(store, identity="6")
    conn = store._get_conn()
    for key, updated in (
        ("normal:expired", "2026-11-15T12:00:00+08:00"),
        ("checkin:expired", "2026-11-15T12:00:00+08:00"),
        ("old-shared", "2026-11-14T12:00:00+08:00"),
        ("normal:valid", "2026-11-15T12:00:01+08:00"),
        ("checkin:valid", "2026-11-15T12:00:01+08:00"),
    ):
        conn.execute("INSERT INTO source_page_cursor VALUES (?, ?, ?, ?)", ("group:2", key, 30, updated))
    conn.commit()
    freeze(store, "2026-11-16T12:00:00+08:00")
    assert await store.cleanup_old_days() == 5
    assert await store.cleanup_old_days() == 0
    assert {row[0] for row in conn.execute("SELECT source_key FROM source_page_cursor")} == {"normal:valid", "checkin:valid"}
    assert await store.get_used_illust_ids("group:1", "any") == {"5", "6"}
    assert await store.get_used_illust_ids("group:1", "any", feature="checkin") == {"2", "4"}
    assert not await claim(store, identity="6", ignore_history=True)


@pytest.mark.asyncio
async def test_paging_writes_trigger_throttled_cleanup_without_deleting_valid_cursors(store):
    from unittest.mock import patch

    await store.advance_page_offset("group:1", "old", 30)
    freeze(store, "2026-11-17T00:00:00+08:00")
    with patch("astrbot_plugin_get_px.pixiv.index.time.monotonic", return_value=store._last_cleanup_ts + 3600):
        await store.advance_page_offset("group:2", "new", 30, feature="checkin")
    assert [tuple(row) for row in store._get_conn().execute("SELECT scope, source_key FROM source_page_cursor")] == [("group:2", "checkin:new")]


@pytest.mark.asyncio
async def test_same_day_expired_claim_renews_before_physical_cleanup(store):
    freeze(store, "2026-11-15T12:00:00+08:00")
    assert await claim(store)
    freeze(store, "2026-11-15T12:10:00+08:00")
    assert not await store.get_used_illust_ids("group:1", "any")
    assert store._get_conn().execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 1
    assert await claim(store)
    assert not await claim(store, ignore_history=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_cleanup", ["release", "record"])
async def test_expired_request_cannot_release_a_new_owners_claim(store, old_cleanup):
    freeze(store, "2026-11-15T12:00:00+08:00")
    assert await claim(store, owner="first-request")
    freeze(store, "2026-11-15T12:09:59+08:00")
    assert not await claim(store, owner="second-request")
    freeze(store, "2026-11-15T12:10:00+08:00")
    assert await claim(store, owner="second-request")
    if old_cleanup == "release":
        await store.release_usage(scope="group:1", source_key="any", illust_id="42", feature="normal_pending", owner="first-request")
    else:
        await record(store, owner="first-request")
    assert not await claim(store, owner="third-request", ignore_history=True)
    await store.release_usage(scope="group:1", source_key="any", illust_id="42", feature="normal_pending", owner="second-request")
    assert await claim(store, owner="third-request", ignore_history=True)


@pytest.mark.asyncio
async def test_first_upgrade_clears_only_image_usage_and_all_paging_keys(store, tmp_path):
    from astrbot_plugin_get_px.checkin import CheckinStore

    await record(store, identity="1")
    await record(store, feature="checkin", identity="2")
    assert await claim(store, identity="3")
    assert await claim(store, feature="checkin_pending", identity="4")
    await store.advance_page_offset("group:1", "pixiv:search:a", 30)
    await store.advance_page_offset("group:1", "pixiv:search:a", 60, feature="checkin")
    await store.add_blacklist_illust(illust_id="99")
    conn = store._get_conn()
    conn.execute("INSERT INTO source_page_cursor VALUES (?, ?, ?, ?)", (
        "group:2", "pixiv:search:old", 90, store.now_iso(),
    ))
    conn.execute("CREATE TABLE content_safety_terms (term TEXT)")
    conn.execute("INSERT INTO content_safety_terms VALUES ('legacy-rule')")
    conn.execute("PRAGMA user_version = 0")  # 模拟尚未切换固定窗口的安装。
    conn.commit()
    store.close()
    card = tmp_path / "checkin_card_cache" / "card.jpg"
    card.parent.mkdir()
    card.write_bytes(b"cached-card")
    checkin = CheckinStore(tmp_path)
    await checkin.get_profile("user")
    await checkin.update_checkin_member(
        user_id="user", coins=123, affection=0, total_days=7, streak_days=2,
    )
    profile = await checkin.find_profile("user")
    reopened = ImageIndexStore(tmp_path)
    try:
        conn = reopened._get_conn()
        assert conn.execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM source_page_cursor").fetchone()[0] == 0
        assert [item["illust_id"] for item in await reopened.list_blacklist_illusts()] == ["99"]
        assert conn.execute("SELECT term FROM content_safety_terms").fetchone()[0] == "legacy-rule"
        assert await checkin.find_profile("user") == profile
        assert card.read_bytes() == b"cached-card"
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_config_save_and_repeated_reload_preserve_post_reset_state(store, tmp_path):
    from astrbot.api import AstrBotConfig
    from astrbot_plugin_get_px.group_safety import SessionSafetyService

    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    config = AstrBotConfig(config_path=str(tmp_path / "config.json"), schema=schema)
    await record(store, identity="1")
    await record(store, feature="checkin", identity="2")
    assert await claim(store, identity="3")
    assert await claim(store, feature="checkin_pending", identity="4")
    await store.advance_page_offset("group:1", "pixiv:search:a", 30)
    await store.advance_page_offset("group:1", "pixiv:search:a", 60, feature="checkin")
    service = SessionSafetyService(config)
    await service.update_global({"global_safety_terms": ["blocked"]})
    await service.save_group({"name": "test", "sessions": ["bot:GroupMessage:1"]})
    config.save_config()
    assert await store.get_used_illust_ids("group:1", "any") == {"1", "3"}
    # 重建索引连接对应重载／进程重启；同时打开第二连接也不能清除新记录。
    for _ in range(2):
        reopened = ImageIndexStore(tmp_path)
        freeze(reopened, store.now_iso())
        try:
            assert await reopened.get_used_illust_ids("group:1", "any") == {"1", "3"}
            assert await reopened.get_used_illust_ids("group:1", "any", feature="checkin") == {"2", "4"}
            assert await reopened.get_page_offset("group:1", "pixiv:search:a") == 30
            assert await reopened.get_page_offset("group:1", "pixiv:search:a", feature="checkin") == 60
        finally:
            reopened.close()
        store.close()


@pytest.mark.parametrize("failure_at", ["cursor", "marker"])
def test_one_time_reset_rolls_back_records_and_marker_on_failure(store, failure_at):
    from astrbot_plugin_get_px.pixiv import usage

    conn = store._get_conn()
    conn.execute("INSERT INTO image_usage VALUES (?, ?, ?, ?, ?, ?, ?)", (
        store.today_key(), "group:1", "old", "42", "normal_pending", "user", store.now_iso(),
    ))
    conn.execute("INSERT INTO source_page_cursor VALUES (?, ?, ?, ?)", (
        "group:1", "old", 30, store.now_iso(),
    ))
    conn.execute("PRAGMA user_version = 0")
    conn.commit()

    def authorize(action, name, value, *_):
        if failure_at == "cursor" and action == sqlite3.SQLITE_DELETE and name == "source_page_cursor":
            return sqlite3.SQLITE_DENY
        if failure_at == "marker" and action == sqlite3.SQLITE_PRAGMA and name == "user_version" and value is not None:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            usage.reset_legacy_state_once(conn)
    finally:
        conn.set_authorizer(None)
    assert not conn.in_transaction
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_page_cursor").fetchone()[0] == 1
    assert usage.reset_legacy_state_once(conn) == (1, 1)
    assert usage.reset_legacy_state_once(conn) is None


@pytest.mark.asyncio
async def test_cleanup_rolls_back_usage_when_cursor_delete_fails(store):
    await record(store)
    await store.advance_page_offset("group:1", "old", 30)
    freeze(store, "2026-11-17T00:00:00+08:00")
    conn = store._get_conn()
    last_cleanup = store._last_cleanup_ts

    def authorize(action, name, *_):
        if action == sqlite3.SQLITE_DELETE and name == "source_page_cursor":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            await store.cleanup_old_days()
    finally:
        conn.set_authorizer(None)
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_page_cursor").fetchone()[0] == 1
    assert store._last_cleanup_ts == last_cleanup
    assert await store.cleanup_old_days() == 2
