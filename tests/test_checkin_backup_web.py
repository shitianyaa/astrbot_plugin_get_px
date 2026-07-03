import io
import sys
import tempfile
import unittest
from pathlib import Path

from quart import Quart
from werkzeug.datastructures import FileStorage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot.api.message_components import File  # noqa: E402
from astrbot_plugin_get_px.checkin import CheckinStore  # noqa: E402
from astrbot_plugin_get_px.checkin import dump_checkin_snapshot_json  # noqa: E402
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
        with tempfile.TemporaryDirectory() as src_tmp, tempfile.TemporaryDirectory() as dst_tmp:
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
            self.assertEqual(payload["profiles"], 1)
            self.assertEqual(payload["records"], 1)
            self.assertTrue(payload["rollback_path"].endswith(".json"))

            imported = await plugin.checkin_store.get_profile("20002")
            replaced = await plugin.checkin_store.get_profile("10001")
            self.assertEqual(imported.total_days, 1)
            self.assertEqual(replaced.total_days, 0)

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
