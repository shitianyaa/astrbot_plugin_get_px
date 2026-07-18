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
    downloader = ImageDownloader()
    # 替换 session 工厂，避免真实建立 aiohttp 连接
    downloader._ensure_session = lambda: _FakeSession(resp)  # type: ignore[assignment]
    return downloader


class ImageDownloaderSizeLimitTest(unittest.IsolatedAsyncioTestCase):
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
        payload = b"\xff\xd8\xff\xe0small-image-bytes"
        resp = _FakeResponse(content_length=len(payload), chunks=[payload])
        downloader = _downloader_with(resp)
        path = await downloader.download("http://x/a.png", timeout=5)
        try:
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.endswith(".png"))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), payload)
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_cleans_partial_temp_file_when_stream_exceeds_limit(self):
        created_paths = []

        def fake_mkstemp(prefix, suffix):
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
                    return str(original_path)
                if url.endswith("large.jpg"):
                    large_path.write_bytes(b"x" * 3)
                    return str(large_path)
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
                    return str(original_path)
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
