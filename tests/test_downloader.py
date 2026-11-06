import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import astrbot_plugin_get_px.pixiv.downloader as dl  # noqa: E402
from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader  # noqa: E402


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(self, status=200, content_length=None, chunks=()):
        self.status = status
        self.content_length = content_length
        self.content = _FakeContent(chunks)


class _FakeGetCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.closed = False
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeGetCtx(self._resp)


def _downloader_with(resp) -> ImageDownloader:
    downloader = ImageDownloader(temp_dir=Path(tempfile.gettempdir()) / "get-px-test-downloads")
    # 替换 session 工厂，避免真实建立 aiohttp 连接
    downloader._ensure_session = lambda: _FakeSession(resp)  # type: ignore[assignment]
    return downloader


class ImageDownloaderSizeLimitTest(unittest.IsolatedAsyncioTestCase):
    async def test_uncertain_file_is_retained_then_automatically_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "get_px_pending.jpg"
            path.write_bytes(b"fixture")
            os.utime(path, (1, 1))
            downloader = ImageDownloader(temp_dir=Path(tmp))
            with mock.patch.object(dl.asyncio, "sleep", new_callable=mock.AsyncMock) as sleep:
                downloader.defer_cleanup(str(path))
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_mtime, 1)
                await asyncio.gather(*tuple(downloader._cleanup_tasks))
                sleep.assert_awaited_once_with(600)
            self.assertFalse(path.exists())
            await downloader.close()

    async def test_close_preserves_pending_file_and_restart_resumes_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "get_px_pending.jpg"
            path.write_bytes(b"fixture")
            downloader = ImageDownloader(temp_dir=Path(tmp))
            downloader.defer_cleanup(str(path))
            await downloader.close()
            self.assertTrue(path.exists())
            self.assertEqual(downloader._cleanup_tasks, set())
            restarted = ImageDownloader(temp_dir=Path(tmp))
            with mock.patch.object(dl, "DELIVERY_FILE_GRACE_SECONDS", 0):
                await restarted.resume_cleanup()
                await asyncio.gather(*tuple(restarted._cleanup_tasks))
            self.assertFalse(path.exists())
            await restarted.close()

    async def test_restart_uses_remaining_grace_and_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fresh, expired, unrelated = [root / name for name in ("get_px_fresh.jpg", "get_px_expired.jpg", "keep.jpg")]
            for path, modified in ((fresh, 9980), (expired, 9000), (unrelated, 9000)):
                path.write_bytes(b"fixture")
                os.utime(path, (modified, modified))
            downloader = ImageDownloader(temp_dir=root)
            with mock.patch.object(dl.time, "time", return_value=10000), mock.patch.object(downloader, "_schedule_cleanup") as schedule:
                await downloader.resume_cleanup()
            scheduled = {Path(call.args[0]).name: call.args[1] for call in schedule.call_args_list}
            self.assertEqual(scheduled, {fresh.name: 580, expired.name: 0})
            self.assertTrue(unrelated.exists())
            await downloader.close()

    async def test_uninitialized_directory_does_not_use_system_temp(self):
        downloader = ImageDownloader()
        with mock.patch.object(downloader, "_ensure_session") as session:
            with self.assertRaisesRegex(RuntimeError, "临时下载目录尚未初始化"):
                await downloader.download("http://x/a.png", timeout=5)
            session.assert_not_called()

    def test_square_medium_url_keeps_square_medium_quality_metadata(self):
        self.assertEqual(
            dl._quality_from_url(
                "https://i.pximg.net/c/360x360_70/img-master/square_medium.jpg"
            ),
            "square_medium",
        )

    async def test_rejects_when_declared_content_length_exceeds_limit(self):
        # 服务器声明的体积已超限：走快速路径，不读 body
        with mock.patch.object(dl, "MAX_DOWNLOAD_BYTES", 10):
            resp = _FakeResponse(content_length=11, chunks=[b"x"])
            downloader = _downloader_with(resp)
            with self.assertRaises(RuntimeError):
                await downloader.download("http://x/a.jpg", timeout=5)

    async def test_rejects_when_streamed_body_exceeds_limit(self):
        # content_length 未声明：分块累计超限应立即中止
        with mock.patch.object(dl, "MAX_DOWNLOAD_BYTES", 10):
            resp = _FakeResponse(
                content_length=None, chunks=[b"123", b"456", b"7890ABC"]
            )
            downloader = _downloader_with(resp)
            with self.assertRaises(RuntimeError):
                await downloader.download("http://x/a.jpg", timeout=5)

    async def test_non_200_status_raises(self):
        resp = _FakeResponse(status=404, chunks=[b"x"])
        downloader = _downloader_with(resp)
        with self.assertRaises(RuntimeError):
            await downloader.download("http://x/a.jpg", timeout=5)

    async def test_small_image_written_to_temp_file_with_suffix(self):
        payload = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
            b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDAT"
            b"x\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\r\xefF\xb8\x00\x00"
            b"\x00\x00IEND\xaeB`\x82"
        )
        resp = _FakeResponse(content_length=len(payload), chunks=[payload])
        downloader = _downloader_with(resp)
        path, size = await downloader.download("http://x/a.png", timeout=5)
        try:
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.endswith(".png"))
            self.assertEqual(Path(path).parent, downloader.temp_dir)
            self.assertEqual(size, len(payload))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), payload)
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_cleans_partial_temp_file_when_stream_exceeds_limit(self):
        created_paths = []

        def fake_mkstemp(prefix, suffix, dir):
            self.assertEqual(dir, downloader.temp_dir)
            path = Path(tmp) / f"{prefix}partial{suffix}"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            created_paths.append(path)
            return fd, str(path)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(dl, "MAX_DOWNLOAD_BYTES", 10):
                resp = _FakeResponse(
                    content_length=None, chunks=[b"12345", b"67890", b"x"]
                )
                downloader = _downloader_with(resp)
                with mock.patch.object(dl.tempfile, "mkstemp", fake_mkstemp):
                    with self.assertRaises(RuntimeError):
                        await downloader.download("http://x/a.jpg", timeout=5)

            self.assertTrue(created_paths)
            self.assertFalse(any(path.exists() for path in created_paths))


def _illust_urls():
    return {
        "meta_single_page": {"original_image_url": "http://x/original.jpg"},
        "image_urls": {
            "large": "http://x/large.jpg",
            "medium": "http://x/medium.jpg",
            "square_medium": "http://x/square_medium.jpg",
        },
    }


class ImageDownloaderDowngradeTest(unittest.IsolatedAsyncioTestCase):
    async def test_download_for_send_removes_original_after_successful_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_path = tmp_path / "original.jpg"
            large_path = tmp_path / "large.jpg"
            downloader = ImageDownloader()

            async def fake_download(url, timeout):
                if url.endswith("original.jpg"):
                    original_path.write_bytes(b"x" * 20)
                    return str(original_path), 20
                if url.endswith("large.jpg"):
                    large_path.write_bytes(b"x" * 3)
                    return str(large_path), 3
                raise RuntimeError(url)

            downloader.download = fake_download  # type: ignore[method-assign]

            path, quality, size = await downloader.download_for_send(
                _illust_urls(),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=10,
                log_context="test",
            )

            self.assertEqual(path, str(large_path))
            self.assertEqual(quality, "large")
            self.assertEqual(size, 3)
            self.assertFalse(original_path.exists())
            self.assertTrue(large_path.exists())

    async def test_download_for_send_removes_original_when_all_downgrades_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            original_path = Path(tmp) / "original.jpg"
            downloader = ImageDownloader()

            async def fake_download(url, timeout):
                if url.endswith("original.jpg"):
                    original_path.write_bytes(b"x" * 20)
                    return str(original_path), 20
                raise RuntimeError("candidate failed")

            downloader.download = fake_download  # type: ignore[method-assign]

            with self.assertRaises(RuntimeError):
                await downloader.download_for_send(
                    _illust_urls(),
                    quality="original",
                    timeout=5,
                    downgrade_limit_bytes=10,
                    log_context="test",
                )

            self.assertFalse(original_path.exists())


if __name__ == "__main__":
    unittest.main()
