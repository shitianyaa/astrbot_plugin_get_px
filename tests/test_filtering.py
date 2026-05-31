import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.main import GetPxPlugin


class FilteringTest(unittest.TestCase):
    def test_filter_manga_removes_manga_from_mixed_results(self):
        illusts = [
            {"id": "1", "type": "manga"},
            {"id": "2", "type": "illust"},
            {"id": "3", "type": "ugoira"},
        ]

        filtered = GetPxPlugin._filter_manga(illusts)

        self.assertEqual([illust["id"] for illust in filtered], ["2", "3"])


if __name__ == "__main__":
    unittest.main()
