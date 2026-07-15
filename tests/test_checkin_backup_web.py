import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from quart import Quart
from werkzeug.datastructures import FileStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.api.message_components import File  # noqa: E402
from astrbot_plugin_get_px.checkin import CheckinStore  # noqa: E402
from astrbot_plugin_get_px.checkin import dump_checkin_snapshot_json  # noqa: E402
from astrbot_plugin_get_px.checkin.commands import (  # noqa: E402
    MAX_CHECKIN_BACKUP_BYTES,
    MAX_CHECKIN_BACKUP_FILES,
)
from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402


class FrozenCheckinStore(CheckinStore):
    def __init__(self, data_dir: Path | str, *, date_key: str = "2026-05-26"):
        self.date_key = date_key
        super().__init__(data_dir)

    def today_key(self) -> str:
        return self.date_key

    def now_iso(self) -> str:
        return f"{self.date_key}T12:00:00+08:00"


class _FakeEvent:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload)

    def chain_result(self, chain):
        return chain

    def plain_result(self, text):
        return text


class CheckinBackupWebTest(unittest.IsolatedAsyncioTestCase):
    async def test_backup_retention_is_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(tmp)
            plugin.checkin_store = FrozenCheckinStore(tmp)
            snapshot = await plugin.checkin_store.export_snapshot()

            for index in range(MAX_CHECKIN_BACKUP_FILES + 5):
                plugin._write_checkin_snapshot_file(
                    snapshot, prefix=f"checkin-export-{index}"
                )

            backups = list((Path(tmp) / "checkin_backups").glob("*.json"))
            self.assertEqual(len(backups), MAX_CHECKIN_BACKUP_FILES)

    async def test_uploaded_snapshot_stops_reading_after_size_limit(self):
        class OversizedStream(io.BytesIO):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(tmp)
            stream = OversizedStream(b"x" * (MAX_CHECKIN_BACKUP_BYTES * 2))
            upload = FileStorage(stream=stream, filename="large.json")

            with self.assertRaisesRegex(ValueError, "不能超过 5 MiB"):
                await plugin._read_uploaded_file_bytes(upload)

            self.assertLess(stream.tell(), MAX_CHECKIN_BACKUP_BYTES * 2)
            self.assertFalse(list((Path(tmp) / "checkin_backups").glob(".upload-*")))

    async def test_handle_checkin_export_sends_only_file_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(tmp)
            plugin.checkin_store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            await plugin.checkin_store.checkin(
                user_id="10001",
                username="tester",
                bot_name="neko",
            )
            event = _FakeEvent()

            result = await plugin._handle_checkin_export(event)

            self.assertIsNone(result)
            self.assertEqual(len(event.sent), 1)
            self.assertEqual(len(event.sent[0]), 1)
            self.assertIsInstance(event.sent[0][0], File)

    async def test_web_checkin_import_accepts_exported_snapshot(self):
        with (
            tempfile.TemporaryDirectory() as src_tmp,
            tempfile.TemporaryDirectory() as dst_tmp,
        ):
            source = FrozenCheckinStore(src_tmp, date_key="2026-05-26")
            await source.checkin(user_id="20002", username="source", bot_name="neko")
            snapshot_text = dump_checkin_snapshot_json(await source.export_snapshot())

            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(dst_tmp)
            plugin.checkin_store = FrozenCheckinStore(dst_tmp, date_key="2026-05-26")
            await plugin.checkin_store.checkin(
                user_id="10001",
                username="target",
                bot_name="neko",
            )
            app = Quart(__name__)
            app.add_url_rule(
                "/checkin-import",
                view_func=plugin._web_checkin_import,
                methods=["POST"],
            )

            with patch("astrbot_plugin_get_px.plugin_api.api.logger") as mock_logger:
                async with app.test_app():
                    client = app.test_client()
                    response = await client.post(
                        "/checkin-import",
                        files={
                            "file": FileStorage(
                                stream=io.BytesIO(snapshot_text.encode("utf-8")),
                                filename="checkin-export.json",
                                name="file",
                                content_type="application/json",
                            )
                        },
                    )
                    payload = await response.get_json()

            self.assertEqual(response.status_code, 200)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["users"], 1)
            self.assertEqual(payload["records"], 1)
            self.assertTrue(payload["rollback_file"].endswith(".json"))
            self.assertNotIn("rollback_path", payload)

            imported = await plugin.checkin_store.get_profile("20002")
            replaced = await plugin.checkin_store.get_profile("10001")
            self.assertEqual(imported.total_days, 1)
            self.assertEqual(replaced.total_days, 0)
            log_text = "\n".join(
                str(call.args[0])
                for method in (mock_logger.info, mock_logger.warning)
                for call in method.call_args_list
            )
            self.assertIn("开始导入签到 JSON 备份", log_text)
            self.assertIn("现有签到数据已备份", log_text)
            self.assertIn("现有签到数据已由 JSON 备份覆盖", log_text)

    async def test_web_checkin_import_rejects_old_snapshot_version(self):
        with (
            tempfile.TemporaryDirectory() as src_tmp,
            tempfile.TemporaryDirectory() as dst_tmp,
        ):
            source = FrozenCheckinStore(src_tmp, date_key="2026-05-26")
            await source.checkin(user_id="20002", username="source", bot_name="neko")
            legacy = await source.export_snapshot()
            legacy["schema_version"] = 5
            snapshot_text = json.dumps(legacy, ensure_ascii=False)

            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(dst_tmp)
            plugin.checkin_store = FrozenCheckinStore(dst_tmp, date_key="2026-05-26")
            app = Quart(__name__)
            app.add_url_rule(
                "/checkin-import-old",
                view_func=plugin._web_checkin_import,
                methods=["POST"],
            )

            async with app.test_app():
                client = app.test_client()
                response = await client.post(
                    "/checkin-import-old",
                    files={
                        "file": FileStorage(
                            stream=io.BytesIO(snapshot_text.encode("utf-8")),
                            filename="checkin-export-old.json",
                            name="file",
                            content_type="application/json",
                        )
                    },
                )
                payload = await response.get_json()

            self.assertEqual(response.status_code, 400)
            self.assertFalse(payload["success"])
            self.assertIn("不支持的签到备份版本", payload["error"])

    async def test_web_checkin_import_rejects_invalid_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(tmp)
            plugin.checkin_store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            app = Quart(__name__)
            app.add_url_rule(
                "/checkin-import",
                view_func=plugin._web_checkin_import,
                methods=["POST"],
            )

            async with app.test_app():
                client = app.test_client()
                response = await client.post(
                    "/checkin-import",
                    files={
                        "file": FileStorage(
                            stream=io.BytesIO(b'{"schema_version":999}'),
                            filename="broken.json",
                            name="file",
                            content_type="application/json",
                        )
                    },
                )
                payload = await response.get_json()

            self.assertEqual(response.status_code, 400)
            self.assertFalse(payload["success"])
            self.assertIn("不支持的签到备份版本", payload["error"])

    async def test_web_checkin_import_rejects_oversized_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = object.__new__(GetPxPlugin)
            plugin.data_dir = Path(tmp)
            plugin.checkin_store = FrozenCheckinStore(tmp, date_key="2026-05-26")
            app = Quart(__name__)
            app.add_url_rule(
                "/checkin-import-large",
                view_func=plugin._web_checkin_import,
                methods=["POST"],
            )

            async with app.test_app():
                client = app.test_client()
                response = await client.post(
                    "/checkin-import-large",
                    files={
                        "file": FileStorage(
                            stream=io.BytesIO(b"x" * (MAX_CHECKIN_BACKUP_BYTES + 1)),
                            filename="large.json",
                            name="file",
                            content_type="application/json",
                        )
                    },
                )
                payload = await response.get_json()

            self.assertEqual(response.status_code, 400)
            self.assertEqual(payload["error"], "签到备份文件不能超过 5 MiB")
            self.assertFalse(list((Path(tmp) / "checkin_backups").glob(".upload-*")))
