from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from quart import Quart

from plugin_api.api import PluginWebApi


@pytest.mark.asyncio
async def test_cache_storage_and_clear():
    with tempfile.TemporaryDirectory() as tmp_root:
        cache_dir = Path(tmp_root) / "card_cache"
        cache_dir.mkdir()
        test_card = cache_dir / "card_1.jpg"
        test_card.write_bytes(b"dummy_card_data_12345")

        # Create a get_px_ temp file inside the test-owned tmp_root
        # so the glob never touches the real system temp directory
        plugin_tmp = Path(tmp_root) / "temp"
        plugin_tmp.mkdir()
        temp_img = plugin_tmp / "get_px_test.jpg"
        temp_img.write_bytes(b"dummy_temp_image_data_67890")
        unrelated = plugin_tmp / "keep.txt"
        unrelated.write_text("not a download", encoding="utf-8")

        plugin = SimpleNamespace(
            checkin_cache=SimpleNamespace(root=cache_dir),
            data_dir=Path(tmp_root),
            log_prefix="[Test]",
        )
        api = PluginWebApi(
            plugin,
            plugin_name="test_plugin",
            log_prefix="[Test]",
            internal_error_message="Error",
        )

        app = Quart(__name__)
        async with app.app_context():
            with patch("tempfile.gettempdir", side_effect=AssertionError("must not scan system temp")):
                # 1. Test get stats
                response = await api.cache_storage()
                data = await response.get_json()
                assert data["success"] is True
                assert data["card_cache_bytes"] == len(b"dummy_card_data_12345")
                assert data["card_cache_count"] == 1
                assert data["temp_download_bytes"] == len(b"dummy_temp_image_data_67890")
                assert data["temp_download_count"] == 1
                assert data["total_count"] == 2
                assert "KB" in data["total_human"] or "B" in data["total_human"]

                # 2. Test clear (file is old enough since mtime is at write time)
                # Make the temp file appear stale by backdating its mtime
                import os
                stale_ts = 0
                os.utime(temp_img, (stale_ts, stale_ts))
                active = plugin_tmp / "get_px_active.jpg"
                active.write_bytes(b"active download")

                clear_response = await api.cache_storage_clear()
                clear_data = await clear_response.get_json()
                assert clear_data["success"] is True
                assert clear_data["freed_bytes"] >= len(b"dummy_card_data_12345") + len(b"dummy_temp_image_data_67890")
                assert clear_data["deleted_files"] >= 2
                assert clear_data.get("skipped_active", 0) == 1
                assert active.exists()
                assert unrelated.exists()
                assert not test_card.exists()
                assert not temp_img.exists()

                # 3. Test after clear
                after_response = await api.cache_storage()
                after_data = await after_response.get_json()
                assert after_data["card_cache_count"] == 0
                assert after_data["card_cache_bytes"] == 0
