"""图片使用记录的固定窗口与原子占用；不依赖图源、标签或调用入口。"""

from datetime import datetime, timedelta

NORMAL_WINDOW_SECONDS = 600
CHECKIN_WINDOW_DAYS = 2
PAGE_CURSOR_WINDOW_SECONDS = 24 * 60 * 60


def reset_legacy_state_once(conn) -> tuple[int, int] | None:
    """图片索引的 user_version=0 表示尚未完成固定窗口切换。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] != 0:
            conn.commit()
            return None
        removed_usage = conn.execute("DELETE FROM image_usage").rowcount
        removed_cursors = conn.execute("DELETE FROM source_page_cursor").rowcount
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        return removed_usage, removed_cursors
    except Exception:
        conn.rollback()
        raise


def window_start(now: datetime, feature: str) -> datetime:
    if feature == "checkin":
        return (now - timedelta(days=CHECKIN_WINDOW_DAYS - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    if feature == "normal":
        return now - timedelta(seconds=NORMAL_WINDOW_SECONDS)
    raise ValueError("未知图片业务类别")


def page_cursor_cutoff(now: datetime) -> datetime:
    return now - timedelta(seconds=PAGE_CURSOR_WINDOW_SECONDS)


def usage_id(value: object) -> str:
    """Pixiv 首张 ID 和 Lolicon 的 PID:0 表示同一页，其余页保持独立。"""
    text = str(value or "")
    return text[:-2] if text.endswith(":0") else text


def id_aliases(value: str) -> tuple[str, str]:
    identity = usage_id(value)
    return identity, identity + ":0" if ":" not in identity else identity


def pending_key(feature: str, owner: str) -> str:
    key = feature.removesuffix("_pending") + ":inflight"
    return f"{key}:{owner}" if owner else key


def cutoff_day(date_key: str) -> str:
    return (datetime.fromisoformat(date_key).date() - timedelta(days=CHECKIN_WINDOW_DAYS - 1)).isoformat()


def active_records(date_key: str, now: str, feature: str, ignore_history=False):
    window = window_start(datetime.fromisoformat(now), "normal").isoformat(timespec="seconds")
    return (
        "date_key BETWEEN ? AND ? AND (? OR sent_at > ?) AND (feature = ? OR (feature = ? AND ?))",
        (cutoff_day(date_key), date_key, feature == "checkin", window,
         feature + "_pending", feature, not ignore_history),
    )


def used_ids(conn, date_key: str, now: str, scope: str, feature: str, ignore_history=False) -> set[str]:
    condition, params = active_records(date_key, now, feature, ignore_history)
    return {usage_id(row[0]) for row in conn.execute(
        f"SELECT illust_id FROM image_usage WHERE scope = ? AND {condition}",
        (scope, *params),
    )}


def claim(conn, date_key, scope, identity, feature, user_id, now, ignore_history, owner=""):
    family = feature.removesuffix("_pending")
    condition, params = active_records(date_key, now, family, ignore_history)
    try:
        conn.execute("BEGIN IMMEDIATE")
        occupied = conn.execute(
            f"SELECT 1 FROM image_usage WHERE scope = ? AND illust_id IN (?, ?) AND {condition} LIMIT 1",
            (scope, *id_aliases(identity), *params),
        ).fetchone()
        if occupied:
            conn.rollback()
            return False
        conn.execute(
            """INSERT INTO image_usage (date_key, scope, source_key, illust_id, feature, user_id, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_key, scope, source_key, illust_id) DO UPDATE SET
            feature = excluded.feature, user_id = excluded.user_id, sent_at = excluded.sent_at""",
            (date_key, scope, pending_key(feature, owner), usage_id(identity), feature, user_id, now),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def record(conn, date_key, scope, identity, feature, user_id, now, keep_pending=False, owner=""):
    try:
        conn.execute(
            """INSERT INTO image_usage (date_key, scope, source_key, illust_id, feature, user_id, sent_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_key, scope, source_key, illust_id) DO UPDATE SET
            feature = excluded.feature, user_id = excluded.user_id, sent_at = excluded.sent_at""",
            (date_key, scope, feature, usage_id(identity), feature, user_id, now),
        )
        if not keep_pending:
            conn.execute(
                "DELETE FROM image_usage WHERE scope = ? AND illust_id IN (?, ?) AND feature = ? AND (? = '' OR source_key = ?)",
                (scope, *id_aliases(identity), feature + "_pending", owner, pending_key(feature, owner)),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def release(conn, date_key, scope, identity, feature, owner=""):
    try:
        conn.execute(
            "DELETE FROM image_usage WHERE scope = ? AND illust_id IN (?, ?) AND feature = ? AND date_key BETWEEN ? AND ? AND (? = '' OR source_key = ?)",
            (scope, *id_aliases(identity), feature, cutoff_day(date_key), date_key, owner, pending_key(feature, owner)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def cleanup(conn, date_key, now) -> tuple[int, int]:
    cutoff = window_start(datetime.fromisoformat(now), "normal").isoformat(timespec="seconds")
    try:
        cursor = conn.execute(
            "DELETE FROM image_usage WHERE date_key < ? OR (feature IN ('normal', 'normal_pending') AND sent_at <= ?)",
            (cutoff_day(date_key), cutoff),
        )
        removed_usage = max(0, cursor.rowcount)
        cursor = conn.execute(
            "DELETE FROM source_page_cursor WHERE updated_at <= ?",
            (page_cursor_cutoff(datetime.fromisoformat(now)).isoformat(timespec="seconds"),),
        )
        conn.commit()
        return removed_usage, max(0, cursor.rowcount)
    except Exception:
        conn.rollback()
        raise
