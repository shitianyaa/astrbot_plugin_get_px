"""图片使用记录的固定窗口与原子占用；不依赖图源、标签或调用入口。"""

from datetime import datetime, timedelta

NORMAL_WINDOW_SECONDS = 600
CHECKIN_WINDOW_DAYS = 2


def usage_id(value: object) -> str:
    """Pixiv 首张 ID 和 Lolicon 的 PID:0 表示同一页，其余页保持独立。"""
    text = str(value or "")
    return text[:-2] if text.endswith(":0") else text


def id_aliases(value: str) -> tuple[str, str]:
    identity = usage_id(value)
    return identity, identity + ":0" if ":" not in identity else identity


def cutoff_day(date_key: str) -> str:
    return (datetime.fromisoformat(date_key).date() - timedelta(days=CHECKIN_WINDOW_DAYS - 1)).isoformat()


def active_records(date_key: str, now: str, feature: str, ignore_history=False):
    window = (datetime.fromisoformat(now) - timedelta(seconds=NORMAL_WINDOW_SECONDS)).isoformat(timespec="seconds")
    return (
        "date_key BETWEEN ? AND ? AND (feature = ? OR (feature = ? AND ? AND (? OR sent_at > ?)))",
        (cutoff_day(date_key), date_key, feature + "_pending", feature,
         not ignore_history, feature == "checkin", window),
    )


def used_ids(conn, date_key: str, now: str, scope: str, feature: str, ignore_history=False) -> set[str]:
    condition, params = active_records(date_key, now, feature, ignore_history)
    return {usage_id(row[0]) for row in conn.execute(
        f"SELECT illust_id FROM image_usage WHERE scope = ? AND {condition}",
        (scope, *params),
    )}


def claim(conn, date_key, scope, identity, feature, user_id, now, ignore_history):
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
            "INSERT INTO image_usage (date_key, scope, source_key, illust_id, feature, user_id, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date_key, scope, family + ":inflight", usage_id(identity), feature, user_id, now),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def record(conn, date_key, scope, identity, feature, user_id, now, keep_pending=False):
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
                "DELETE FROM image_usage WHERE scope = ? AND illust_id IN (?, ?) AND feature = ?",
                (scope, *id_aliases(identity), feature + "_pending"),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def release(conn, date_key, scope, identity, feature):
    try:
        conn.execute(
            "DELETE FROM image_usage WHERE scope = ? AND illust_id IN (?, ?) AND feature = ? AND date_key BETWEEN ? AND ?",
            (scope, *id_aliases(identity), feature, cutoff_day(date_key), date_key),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def cleanup(conn, date_key, now):
    cutoff = (datetime.fromisoformat(now) - timedelta(seconds=NORMAL_WINDOW_SECONDS)).isoformat(timespec="seconds")
    try:
        cursor = conn.execute(
            "DELETE FROM image_usage WHERE date_key < ? OR (feature = 'normal' AND sent_at <= ?)",
            (cutoff_day(date_key), cutoff),
        )
        conn.commit()
        return max(0, cursor.rowcount)
    except Exception:
        conn.rollback()
        raise
