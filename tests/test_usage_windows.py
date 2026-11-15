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
async def test_pending_outlives_ten_minute_window_and_blocks_explicit_retry(store, with_completed_record):
    assert await claim(store)
    if with_completed_record:
        await record(store, keep_pending=True)
    freeze(store, "2026-11-16T00:10:00+08:00")
    assert not await claim(store, ignore_history=True)
    assert await store.get_used_illust_ids("group:1", "any", ignore_history=True) == {"42"}
    await store.release_usage(scope="group:1", source_key="any", illust_id="42:0", feature="normal_pending")
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
async def test_cleanup_preserves_checkin_and_pending_while_expiring_normal_history(store):
    await record(store, identity="1")
    await record(store, feature="checkin", identity="2")
    assert await claim(store, identity="3")
    freeze(store, "2026-11-16T00:10:00+08:00")
    await store.cleanup_old_days()
    assert await store.get_used_illust_ids("group:1", "any") == {"3"}
    assert await store.get_used_illust_ids("group:1", "any", feature="checkin") == {"2"}
