from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from .backup_store import BackupStoreMixin
from .feature_store import FeatureStoreMixin
from .models import SHANGHAI_TZ
from .record_store import RecordStoreMixin
from .schema import SchemaMixin


class CheckinStore(
    RecordStoreMixin,
    FeatureStoreMixin,
    BackupStoreMixin,
    SchemaMixin,
):
    def __init__(self, data_dir: Path | str):
        self._db_path = Path(data_dir) / "checkin.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_db()

    @staticmethod
    def today_key() -> str:
        return datetime.now(SHANGHAI_TZ).date().isoformat()

    @staticmethod
    def now_iso() -> str:
        return datetime.now(SHANGHAI_TZ).isoformat(timespec="seconds")
