import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import astrbot_plugin_get_px.pixiv.downloader as dl  # noqa: E402
import astrbot_plugin_get_px.pixiv.proxy as proxy_utils  # noqa: E402
from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader  # noqa: E402


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            yield chunk


class _FakeResponse:
    def __init__(
        self, status=200, content_length=None, chunks=(), content_type=""
    ):
        self.status = status
        self.content_length = content_length
        self.content = _FakeContent(chunks)
        self.content_type = content_type


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

    async def close(self):
        self.closed = True


def _downloader_with(resp) -> ImageDownloader:
    downloader = ImageDownloader()
    session = _FakeSession(resp)
    # 替换 session 工厂，避免真实建立 aiohttp 连接
    downloader._ensure_session = lambda _proxy: (session, None)  # type: ignore[assignment]
    return downloader


class ImageDownloaderProxyTest(unittest.IsolatedAsyncioTestCase):
    def test_image_reverse_proxy_resolution(self):
        self.assertEqual(proxy_utils.resolve_pixiv_image_proxy_host("", ""), "")
        self.assertEqual(
            proxy_utils.resolve_pixiv_image_proxy_host(
                "socks5://127.0.0.1:1080", ""
            ),
            "i.pixiv.re",
        )
        self.assertEqual(
            proxy_utils.resolve_pixiv_image_proxy_host(
                "socks5://127.0.0.1:1080", "images.example.com"
            ),
            "images.example.com",
        )
        self.assertEqual(
            proxy_utils.resolve_pixiv_image_proxy_host("", "images.example.com"),
            "images.example.com",
        )

    async def test_http_proxy_is_passed_to_request(self):
        payload = b"image"
        session = _FakeSession(
            _FakeResponse(content_length=len(payload), chunks=[payload])
        )
        downloader = ImageDownloader()

        with mock.patch.object(dl.aiohttp, "ClientSession", return_value=session):
            path = await downloader.download(
                "https://i.pximg.net/image.jpg",
                proxy="http://127.0.0.1:7890",
                timeout=5,
            )

        try:
            self.assertEqual(
                session.calls[0][1]["proxy"], "http://127.0.0.1:7890"
            )
        finally:
            os.remove(path)
            await downloader.close()

    async def test_socks5h_proxy_uses_remote_dns_connector(self):
        payload = b"image"
        session = _FakeSession(
            _FakeResponse(content_length=len(payload), chunks=[payload])
        )
        connector = object()
        downloader = ImageDownloader()

        with (
            mock.patch.object(
                dl.ProxyConnector, "from_url", return_value=connector
            ) as connector_factory,
            mock.patch.object(dl.aiohttp, "ClientSession", return_value=session),
        ):
            path = await downloader.download(
                "https://i.pximg.net/image.jpg",
                proxy="socks5h://127.0.0.1:1080",
                timeout=5,
            )

        try:
            connector_factory.assert_called_once_with(
                "socks5://127.0.0.1:1080", rdns=True
            )
            self.assertNotIn("proxy", session.calls[0][1])
        finally:
            os.remove(path)
            await downloader.close()

    async def test_equivalent_socks5_aliases_reuse_session(self):
        session = _FakeSession(_FakeResponse())
        downloader = ImageDownloader()

        with (
            mock.patch.object(dl.ProxyConnector, "from_url", return_value=object()),
            mock.patch.object(
                dl.aiohttp, "ClientSession", return_value=session
            ) as session_factory,
        ):
            first, _ = downloader._ensure_session("socks5h://127.0.0.1:1080")
            second, _ = downloader._ensure_session("socks5://127.0.0.1:1080")

        self.assertIs(first, second)
        session_factory.assert_called_once()
        await downloader.close()
        self.assertTrue(session.closed)

    async def test_image_reverse_proxy_rewrites_url_and_bypasses_socks(self):
        payload = b"image"
        session = _FakeSession(
            _FakeResponse(content_length=len(payload), chunks=[payload])
        )
        downloader = ImageDownloader()
        source_url = "https://i.pximg.net/img-master/example.jpg"

        with (
            mock.patch.object(dl.aiohttp, "ClientSession", return_value=session),
            mock.patch.object(dl.ProxyConnector, "from_url") as connector_factory,
        ):
            path = await downloader.download(
                source_url,
                proxy="socks5h://127.0.0.1:1080",
                timeout=5,
                reverse_proxy_host="i.pixiv.re",
            )

        try:
            self.assertEqual(
                session.calls[0][0],
                "https://i.pixiv.re/img-master/example.jpg",
            )
            self.assertNotIn("proxy", session.calls[0][1])
            connector_factory.assert_not_called()
        finally:
            os.remove(path)
            await downloader.close()

    async def test_image_reverse_proxy_does_not_rewrite_unrelated_host(self):
        payload = b"image"
        session = _FakeSession(
            _FakeResponse(content_length=len(payload), chunks=[payload])
        )
        downloader = ImageDownloader()

        with mock.patch.object(dl.aiohttp, "ClientSession", return_value=session):
            path = await downloader.download(
                "https://cdn.example.com/image.jpg",
                proxy="http://127.0.0.1:7890",
                timeout=5,
                reverse_proxy_host="i.pixiv.re",
            )

        try:
            self.assertEqual(session.calls[0][0], "https://cdn.example.com/image.jpg")
            self.assertEqual(
                session.calls[0][1]["proxy"], "http://127.0.0.1:7890"
            )
        finally:
            os.remove(path)
            await downloader.close()


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
                await downloader.download("http://x/a.jpg", proxy="", timeout=5)

    async def test_rejects_when_streamed_body_exceeds_limit(self):
        # content_length 未声明：分块累计超限应立即中止
        with mock.patch.object(dl, "MAX_DOWNLOAD_BYTES", 10):
            resp = _FakeResponse(
                content_length=None, chunks=[b"123", b"456", b"7890ABC"]
            )
            downloader = _downloader_with(resp)
            with self.assertRaises(RuntimeError):
                await downloader.download("http://x/a.jpg", proxy="", timeout=5)

    async def test_non_200_status_raises(self):
        resp = _FakeResponse(status=404, chunks=[b"x"])
        downloader = _downloader_with(resp)
        with self.assertRaises(RuntimeError):
            await downloader.download("http://x/a.jpg", proxy="", timeout=5)

    async def test_small_image_written_to_temp_file_with_suffix(self):
        payload = b"\xff\xd8\xff\xe0small-image-bytes"
        resp = _FakeResponse(content_length=len(payload), chunks=[payload])
        downloader = _downloader_with(resp)
        path = await downloader.download("http://x/a.png", proxy="", timeout=5)
        try:
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.endswith(".png"))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), payload)
        finally:
            if os.path.exists(path):
                os.remove(path)

    async def test_response_content_type_overrides_url_suffix(self):
        payload = b"webp-image-bytes"
        resp = _FakeResponse(
            content_length=len(payload),
            chunks=[payload],
            content_type="image/webp",
        )
        downloader = _downloader_with(resp)

        path = await downloader.download("http://x/a.jpg", proxy="", timeout=5)
        try:
            self.assertTrue(path.endswith(".webp"))
        finally:
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
                        await downloader.download("http://x/a.jpg", proxy="", timeout=5)

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

            async def fake_download(url, proxy, timeout, reverse_proxy_host=""):
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
                proxy="",
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

            async def fake_download(url, proxy, timeout, reverse_proxy_host=""):
                if url.endswith("original.jpg"):
                    original_path.write_bytes(b"x" * 20)
                    return str(original_path)
                raise RuntimeError("candidate failed")

            downloader.download = fake_download  # type: ignore[method-assign]

            with self.assertRaises(RuntimeError):
                await downloader.download_for_send(
                    _illust_urls(),
                    quality="original",
                    proxy="",
                    timeout=5,
                    downgrade_limit_bytes=10,
                    log_context="test",
                )

            self.assertFalse(original_path.exists())


if __name__ == "__main__":
    unittest.main()
