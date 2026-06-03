import tempfile
import unittest
from pathlib import Path

from PIL import Image

from image_history import ImageAssetManager


class FakeEvent:
    unified_msg_origin = "aiocqhttp:group:20001"

    def get_group_id(self):
        return "20001"

    def get_sender_id(self):
        return "10001"

    def get_platform_name(self):
        return "aiocqhttp"


class ImageAssetManagerTest(unittest.IsolatedAsyncioTestCase):
    async def test_checkin_background_records_thumbnail(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = Path(tmp) / "background.jpg"
            Image.new("RGB", (880, 400), (245, 158, 11)).save(image_path)
            manager = ImageAssetManager(tmp)

            await manager.record_sent(
                illust={
                    "id": "123456",
                    "title": "checkin bg",
                    "user": {"id": "42", "name": "artist"},
                    "width": 880,
                    "height": 400,
                    "page_count": 1,
                    "tags": [{"name": "背景"}],
                },
                image_path=str(image_path),
                event=FakeEvent(),
                source="checkin",
                quality="large",
                file_size=image_path.stat().st_size,
            )

            records = await manager.list_records()
            thumb_path = await manager.get_thumbnail_path(records[0]["record_id"])

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "checkin")
            self.assertEqual(records[0]["illust_id"], "123456")
            self.assertIsNotNone(thumb_path)
            self.assertTrue(thumb_path.is_file())

    async def test_corrupt_history_is_backed_up_before_new_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "image_history"
            history_path = root / "image_history.json"
            manager = ImageAssetManager(tmp)
            history_path.write_text("{not json", encoding="utf-8")

            records = await manager.list_records()

            self.assertEqual(records, [])
            backups = list(root.glob("image_history.json.corrupt.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")
            self.assertFalse(history_path.exists())

            image_path = Path(tmp) / "background.jpg"
            Image.new("RGB", (880, 400), (245, 158, 11)).save(image_path)
            await manager.record_sent(
                illust={
                    "id": "123456",
                    "title": "checkin bg",
                    "user": {"id": "42", "name": "artist"},
                    "width": 880,
                    "height": 400,
                    "page_count": 1,
                    "tags": [{"name": "背景"}],
                },
                image_path=str(image_path),
                event=FakeEvent(),
                source="checkin",
                quality="large",
                file_size=image_path.stat().st_size,
            )

            self.assertTrue(history_path.exists())
            self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")
            self.assertEqual(len(await manager.list_records()), 1)


if __name__ == "__main__":
    unittest.main()
