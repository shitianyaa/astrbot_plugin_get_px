"""固定去重窗口、旧记录兼容、跨来源身份和未知送达保护。"""

import asyncio
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore


def freeze(store, value):
    now = datetime.fromisoformat(value)
    store.today_key = lambda: now.date().isoformat()
    store.now_iso = lambda: now.isoformat(timespec="seconds")
    store._now_dt = lambda: now


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
async def test_existing_records_are_read_without_rewriting_the_database(store):
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
async def test_cursor_windows_are_separate_and_ignore_legacy_shared_offsets(store):
    scope, key = "group:1", "pixiv:search:洛茜 黑丝"
    conn = store._get_conn()
    conn.execute("INSERT INTO source_page_cursor VALUES (?, ?, ?, ?)", (scope, key, 90, store.now_iso()))
    conn.commit()
    assert await store.get_page_offset(scope, key) == 0
    assert await store.get_page_offset(scope, key, feature="checkin") == 0
    await store.advance_page_offset(scope, key, 30)
    await store.advance_page_offset(scope, key, 60, feature="checkin")
    assert await store.get_page_offset(scope, key) == 30
    assert await store.get_page_offset(scope, key, feature="checkin") == 60
    freeze(store, "2026-11-16T00:04:59+08:00")
    assert await store.get_page_offset(scope, key) == 30
    freeze(store, "2026-11-16T00:05:00+08:00")
    assert await store.get_page_offset(scope, key) == 0
    # 推进也必须忽略过期起点，不能从失效位置继续累加。
    assert await store.advance_page_offset(scope, key, 30) == 30
    assert await store.get_page_offset(scope, key, feature="checkin") == 60
    freeze(store, "2026-11-17T00:00:00+08:00")
    assert await store.get_page_offset(scope, key, feature="checkin") == 0


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
