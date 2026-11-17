"""图片索引分页结构升级保留已标记状态，先备份后提交DDL。"""
import sqlite3
from unittest.mock import patch

import pytest

from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.checkin.schema import upgrade_image_index_schema


async def legacy_index(tmp_path):
    store = ImageIndexStore(tmp_path)
    await store.record_usage(scope="group:1", source_key="old", illust_id="1", feature="normal")
    await store.claim_usage(scope="group:1", source_key="old", illust_id="2", feature="checkin_pending")
    await store.add_blacklist_illust(illust_id="99")
    await store.save_page_cursor("group:1", "pixiv:recommended", {"offset": 60})
    path = store._db_path
    store.close()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE old_cursor (scope TEXT NOT NULL, source_key TEXT NOT NULL, page_offset INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(scope,source_key))")
        conn.execute("INSERT INTO old_cursor SELECT scope,source_key,page_offset,updated_at FROM source_page_cursor")
        conn.execute("DROP TABLE source_page_cursor")
        conn.execute("ALTER TABLE old_cursor RENAME TO source_page_cursor")
        conn.execute("PRAGMA user_version=1")
    return path


@pytest.mark.asyncio
async def test_v1_upgrade_preserves_history_pending_blacklist_and_legacy_offset(tmp_path):
    path = await legacy_index(tmp_path)
    store = ImageIndexStore(tmp_path)
    try:
        assert store._get_conn().execute("PRAGMA user_version").fetchone()[0] == 2
        assert await store.get_used_illust_ids("group:1", "any") == {"1"}
        assert await store.get_used_illust_ids("group:1", "any", feature="checkin") == {"2"}
        assert (await store.list_blacklist_illusts())[0]["illust_id"] == "99"
        assert await store.get_page_cursor("group:1", "pixiv:recommended") == {"offset": 60}
        await store.save_page_cursor("group:1", "pixiv:recommended", {"max_bookmark_id_for_recommend": 123})
    finally:
        store.close()
    backups = list((tmp_path / "image_index_migration_backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 2
        assert "page_params" not in {row[1] for row in backup.execute("PRAGMA table_info(source_page_cursor)")}
    store = ImageIndexStore(path.parent)
    try:
        assert await store.get_page_cursor("group:1", "pixiv:recommended") == {"max_bookmark_id_for_recommend": 123}
        assert len(list((tmp_path / "image_index_migration_backups").glob("*.sqlite3"))) == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_backup_failure_stops_upgrade_without_resetting_v1_data(tmp_path):
    path = await legacy_index(tmp_path)
    connect = sqlite3.connect
    def fail_backup(filename, *args, **kwargs):
        if "image-index-v1-" in str(filename):
            raise OSError("fixture backup failure")
        return connect(filename, *args, **kwargs)
    with patch("astrbot_plugin_get_px.checkin.schema.sqlite3.connect", side_effect=fail_backup):
        with pytest.raises(OSError):
            ImageIndexStore(tmp_path)
    with connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 2
        assert "page_params" not in {row[1] for row in conn.execute("PRAGMA table_info(source_page_cursor)")}


@pytest.mark.asyncio
async def test_marker_failure_rolls_back_cursor_column_and_preserves_records(tmp_path):
    path = await legacy_index(tmp_path)
    with sqlite3.connect(path) as conn:
        def authorize(action, key, value, *_):
            if action == sqlite3.SQLITE_PRAGMA and key == "user_version" and value is not None:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        conn.set_authorizer(authorize)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                upgrade_image_index_schema(conn, path)
        finally:
            conn.set_authorizer(None)
        assert not conn.in_transaction
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM image_usage").fetchone()[0] == 2
        assert "page_params" not in {row[1] for row in conn.execute("PRAGMA table_info(source_page_cursor)")}
        upgrade_image_index_schema(conn, path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"url": "https://example.test"}, {"offset": -1}, {"offset": True}, {"offset": []}])
async def test_invalid_cursor_cannot_replace_a_valid_position(tmp_path, params):
    store = ImageIndexStore(tmp_path)
    try:
        await store.save_page_cursor("group:1", "pixiv:search:cat", {"offset": 30})
        with pytest.raises(ValueError):
            await store.save_page_cursor("group:1", "pixiv:search:cat", params)
        assert await store.get_page_cursor("group:1", "pixiv:search:cat") == {"offset": 30}
    finally:
        store.close()

@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ['{"url":"https://example.test"}', '[]', 'invalid-json'])
async def test_corrupted_cursor_is_removed_instead_of_used(tmp_path, stored):
    store = ImageIndexStore(tmp_path)
    try:
        await store.save_page_cursor("group:1", "pixiv:recommended", {"offset": 30})
        conn = store._get_conn()
        conn.execute("UPDATE source_page_cursor SET page_params=?", (stored,))
        conn.commit()
        assert await store.get_page_cursor("group:1", "pixiv:recommended") is None
        assert conn.execute("SELECT COUNT(*) FROM source_page_cursor").fetchone()[0] == 0
    finally:
        store.close()
