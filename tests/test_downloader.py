import asyncio
from io import BytesIO
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import astrbot_plugin_get_px.pixiv.downloader as dl  # noqa: E402
from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader  # noqa: E402


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, _size):
        for chunk in self._chunks:
            if isinstance(chunk, BaseException):
                raise chunk
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


def _image_bytes(format_name="JPEG") -> bytes:
    output = BytesIO()
    PILImage.new("RGB", (2, 2), (32, 64, 96)).save(output, format=format_name)
    return output.getvalue()


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
        payload = _image_bytes("PNG")
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

    async def test_rejects_200_response_that_is_not_an_image(self):
        payload = b"<html><title>proxy error</title></html>"
        resp = _FakeResponse(content_length=len(payload), chunks=[payload])
        downloader = _downloader_with(resp)

        with self.assertRaisesRegex(RuntimeError, "不是有效图片"):
            await downloader.download("http://x/a.jpg", timeout=5)

    async def test_cleans_partial_temp_file_when_download_is_cancelled(self):
        created_paths = []

        def fake_mkstemp(prefix, suffix):
            path = Path(tmp) / f"{prefix}cancelled{suffix}"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            created_paths.append(path)
            return fd, str(path)

        with tempfile.TemporaryDirectory() as tmp:
            resp = _FakeResponse(chunks=[asyncio.CancelledError()])
            downloader = _downloader_with(resp)
            with mock.patch.object(dl.tempfile, "mkstemp", fake_mkstemp):
                with self.assertRaises(asyncio.CancelledError):
                    await downloader.download("http://x/a.jpg", timeout=5)

            self.assertTrue(created_paths)
            self.assertFalse(any(path.exists() for path in created_paths))

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


class ImageProxyOriginTest(unittest.TestCase):
    def test_parse_multiline_origins_filters_invalid_deduplicates_and_limits(self):
        raw = "\n".join(
            [
                "https://Proxy.Example.com/",
                "https://proxy.example.com",
                "ftp://invalid.example.com",
                "https://user:pass@example.com",
                "https://example.com/path",
                "https://two.example.com:8443",
                "http://three.example.com",
                "https://four.example.com",
                "https://five.example.com",
                "https://six.example.com",
            ]
        )

        with mock.patch.object(dl.logger, "warning") as warning:
            origins = dl.parse_proxy_origins(raw)

        self.assertEqual(
            origins,
            (
                "https://proxy.example.com",
                "https://two.example.com:8443",
                "http://three.example.com",
                "https://four.example.com",
                "https://five.example.com",
            ),
        )
        self.assertEqual(warning.call_count, 2)
        messages = " ".join(str(call) for call in warning.call_args_list)
        self.assertNotIn(
            "user:pass",
            messages,
        )
        self.assertIn("invalid_count=3", messages)
        self.assertIn("sample_lines=3,4,5", messages)
        self.assertIn("仅使用前 5 个有效地址", messages)

    def test_config_summary_only_logs_valid_origin_count(self):
        raw = "https://proxy.example.com\nhttps://user:secret@example.com"

        with (
            mock.patch.object(dl.logger, "debug") as debug,
            mock.patch.object(dl.logger, "warning"),
        ):
            ImageDownloader(raw)

        message = " ".join(str(call) for call in debug.call_args_list)
        self.assertIn("valid_origins=1", message)
        self.assertNotIn("proxy.example.com", message)
        self.assertNotIn("secret", message)

    def test_rewrite_preserves_image_path_query_and_fragment(self):
        self.assertEqual(
            dl.rewrite_image_url_with_origin(
                "https://i.pximg.net/img-original/a.jpg?token=1#page",
                "https://proxy.example.com:8443",
            ),
            "https://proxy.example.com:8443/img-original/a.jpg?token=1#page",
        )

    def test_only_lolicon_allowed_hosts_are_rewritten(self):
        proxies = ("https://proxy.example.com",)
        allowed = list(
            dl.iter_download_urls(
                "https://i.pximg.net/a.jpg",
                source="lolicon",
                proxy_origins=proxies,
            )
        )
        other_host = list(
            dl.iter_download_urls(
                "https://images.example.com/a.jpg",
                source="lolicon",
                proxy_origins=proxies,
            )
        )
        pixiv = list(
            dl.iter_download_urls(
                "https://i.pximg.net/a.jpg",
                source="pixiv",
                proxy_origins=proxies,
            )
        )

        self.assertEqual(
            allowed,
            ["https://proxy.example.com/a.jpg", "https://i.pximg.net/a.jpg"],
        )
        self.assertEqual(other_host, ["https://images.example.com/a.jpg"])
        self.assertEqual(pixiv, ["https://i.pximg.net/a.jpg"])


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

    async def test_download_for_send_keeps_downgrading_until_within_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            paths = {
                "original": tmp_path / "original.jpg",
                "large": tmp_path / "large.jpg",
                "medium": tmp_path / "medium.jpg",
            }
            sizes = {"original": 20, "large": 15, "medium": 3}
            downloader = ImageDownloader()

            async def fake_download(url, timeout):
                quality = Path(url).stem
                path = paths[quality]
                path.write_bytes(b"x" * sizes[quality])
                return str(path)

            downloader.download = fake_download  # type: ignore[method-assign]

            path, quality, size = await downloader.download_for_send(
                _illust_urls(),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=10,
                log_context="test",
            )

            self.assertEqual((path, quality, size), (str(paths["medium"]), "medium", 3))
            self.assertFalse(paths["original"].exists())
            self.assertFalse(paths["large"].exists())
            self.assertTrue(paths["medium"].exists())

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


class ImageDownloaderProxyFallbackTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _proxy_illust(source="lolicon"):
        return {
            "_source": source,
            "meta_single_page": {
                "original_image_url": "https://i.pximg.net/img-original/a.jpg?x=1"
            },
            "image_urls": {
                "large": "https://i.pixiv.re/img-master/large.jpg",
                "medium": "https://i.pixiv.cat/img-master/medium.jpg",
                "square_medium": "https://proxy.pixivel.moe/img-master/thumb.jpg",
            },
        }

    async def test_lolicon_tries_proxies_in_order_before_returned_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.jpg"
            downloader = ImageDownloader(
                "https://proxy-one.example.com\nhttps://proxy-two.example.com"
            )
            calls = []

            async def fake_download(url, timeout):
                calls.append(url)
                if url.startswith("https://proxy-two.example.com"):
                    result_path.write_bytes(b"ok")
                    return str(result_path)
                raise RuntimeError("failed")

            downloader.download = fake_download  # type: ignore[method-assign]
            path, quality, size = await downloader.download_for_send(
                self._proxy_illust(),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=0,
                log_context="test",
            )

            self.assertEqual(path, str(result_path))
            self.assertEqual((quality, size), ("original", 2))
            self.assertEqual(
                calls,
                [
                    "https://proxy-one.example.com/img-original/a.jpg?x=1",
                    "https://proxy-two.example.com/img-original/a.jpg?x=1",
                ],
            )

    async def test_returned_url_fallback_is_logged_without_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.jpg"
            downloader = ImageDownloader("https://proxy.example.com")

            async def fake_download(url, timeout):
                if url.startswith("https://proxy.example.com"):
                    raise RuntimeError(
                        "upstream rejected https://proxy.example.com/a.jpg?token=secret"
                    )
                result_path.write_bytes(b"ok")
                return str(result_path)

            downloader.download = fake_download  # type: ignore[method-assign]
            with (
                mock.patch.object(dl.logger, "debug") as debug,
                mock.patch.object(dl.logger, "info") as info,
            ):
                path, quality, size = await downloader.download_for_send(
                    self._proxy_illust(),
                    quality="original",
                    timeout=5,
                    downgrade_limit_bytes=0,
                    log_context="test",
                )

        self.assertEqual((path, quality, size), (str(result_path), "original", 2))
        messages = " ".join(str(call) for call in debug.call_args_list)
        self.assertIn("reason=runtime_error", messages)
        self.assertIn("尝试 Lolicon 返回地址", messages)
        info_messages = " ".join(str(call) for call in info.call_args_list)
        self.assertIn("图片下载完成", info_messages)
        self.assertIn("画质=原图", info_messages)
        self.assertIn("下载路径=图片源返回地址", info_messages)
        self.assertIn("尝试次数=2", info_messages)
        self.assertIn("大小=0.00KB", info_messages)
        self.assertRegex(info_messages, r"耗时=\d+ms")
        self.assertNotIn("quality=", info_messages)
        self.assertNotIn("route=", info_messages)
        self.assertNotIn("attempts=", info_messages)
        self.assertNotIn("size_bytes=", info_messages)
        self.assertNotIn("elapsed_ms=", info_messages)
        self.assertNotIn("token=secret", messages)
        self.assertNotIn("token=secret", info_messages)

    async def test_failed_quality_falls_back_after_proxies_and_returned_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "large.jpg"
            downloader = ImageDownloader("https://proxy.example.com")
            calls = []

            async def fake_download(url, timeout):
                calls.append(url)
                if url == "https://proxy.example.com/img-master/large.jpg":
                    result_path.write_bytes(b"large")
                    return str(result_path)
                raise RuntimeError("failed")

            downloader.download = fake_download  # type: ignore[method-assign]
            path, quality, size = await downloader.download_for_send(
                self._proxy_illust(),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=0,
                log_context="test",
            )

            self.assertEqual((path, quality, size), (str(result_path), "large", 5))
            self.assertEqual(
                calls,
                [
                    "https://proxy.example.com/img-original/a.jpg?x=1",
                    "https://i.pximg.net/img-original/a.jpg?x=1",
                    "https://proxy.example.com/img-master/large.jpg",
                ],
            )

    async def test_total_download_attempts_are_limited_to_eight(self):
        proxies = "\n".join(
            f"https://proxy-{index}.example.com" for index in range(1, 6)
        )
        downloader = ImageDownloader(proxies)
        calls = []

        async def fake_download(url, timeout):
            calls.append(url)
            raise RuntimeError("failed")

        downloader.download = fake_download  # type: ignore[method-assign]
        with mock.patch.object(dl.logger, "warning") as warning:
            with self.assertRaisesRegex(RuntimeError, "已尝试 8 个地址"):
                await downloader.download_for_send(
                    self._proxy_illust(),
                    quality="original",
                    timeout=5,
                    downgrade_limit_bytes=0,
                    log_context="test",
                )

        self.assertEqual(len(calls), 8)
        self.assertTrue(calls[4].startswith("https://proxy-5.example.com/"))
        self.assertTrue(calls[5].startswith("https://i.pximg.net/"))
        self.assertTrue(calls[6].startswith("https://proxy-1.example.com/"))
        self.assertEqual(calls[7], "https://i.pixiv.re/img-master/large.jpg")
        messages = " ".join(str(call) for call in warning.call_args_list)
        self.assertIn("图片下载尝试预算已耗尽", messages)
        self.assertIn("图片下载最终失败", messages)

    async def test_failure_logs_and_exception_do_not_expose_upstream_details(self):
        downloader = ImageDownloader("https://proxy.example.com")
        secret = "https://proxy.example.com/a.jpg?token=secret"

        async def fake_download(url, timeout):
            raise RuntimeError(f"upstream response included {secret}")

        downloader.download = fake_download  # type: ignore[method-assign]
        with (
            mock.patch.object(dl.logger, "debug") as debug,
            mock.patch.object(dl.logger, "warning") as warning,
        ):
            with self.assertRaises(RuntimeError) as raised:
                await downloader.download_for_send(
                    self._proxy_illust(),
                    quality="original",
                    timeout=5,
                    downgrade_limit_bytes=0,
                    log_context="test",
                )

        messages = " ".join(
            str(call)
            for call in [*debug.call_args_list, *warning.call_args_list]
        )
        self.assertIn("reason=runtime_error", messages)
        self.assertNotIn(secret, messages)
        self.assertNotIn(secret, str(raised.exception))

    async def test_log_context_redacts_urls_and_flattens_newlines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.jpg"
            path.write_bytes(b"ok")
            downloader = ImageDownloader()

            async def fake_download(url, timeout):
                return str(path)

            downloader.download = fake_download  # type: ignore[method-assign]
            with mock.patch.object(dl.logger, "info") as info:
                await downloader.download_for_send(
                    self._proxy_illust(source="pixiv"),
                    quality="original",
                    timeout=5,
                    downgrade_limit_bytes=0,
                    log_context="作品\nhttps://example.com/a.jpg?token=secret",
                )

        messages = " ".join(str(call) for call in info.call_args_list)
        self.assertIn("作品 [url]", messages)
        self.assertIn("图片下载完成", messages)
        self.assertIn("画质=原图", messages)
        self.assertIn("下载路径=直连地址", messages)
        self.assertIn("尝试次数=1", messages)
        self.assertNotIn("token=secret", messages)
        self.assertNotIn("作品\\n", messages)

    async def test_all_timeout_candidates_preserve_timeout_classification(self):
        downloader = ImageDownloader("https://proxy.example.com")

        async def fake_download(url, timeout):
            raise asyncio.TimeoutError("endpoint included ?token=secret")

        downloader.download = fake_download  # type: ignore[method-assign]
        with self.assertRaisesRegex(asyncio.TimeoutError, "已尝试"):
            await downloader.download_for_send(
                self._proxy_illust(),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=0,
                log_context="test",
            )

    async def test_attempt_budget_preserves_returned_url_for_lower_quality(self):
        proxies = "\n".join(
            f"https://proxy-{index}.example.com" for index in range(1, 6)
        )
        downloader = ImageDownloader(proxies)
        calls = []

        async def fake_download(url, timeout):
            calls.append(url)
            if url == "https://i.pixiv.re/img-master/large.jpg":
                path = Path(tmp) / "large.jpg"
                path.write_bytes(b"ok")
                return str(path)
            raise RuntimeError("failed")

        with tempfile.TemporaryDirectory() as tmp:
            downloader.download = fake_download  # type: ignore[method-assign]
            path, quality, size = await downloader.download_for_send(
                self._proxy_illust(),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=0,
                log_context="test",
            )

        self.assertEqual((quality, size), ("large", 2))
        self.assertTrue(path.endswith("large.jpg"))
        self.assertEqual(calls[-1], "https://i.pixiv.re/img-master/large.jpg")
        self.assertLessEqual(len(calls), 8)

    async def test_pixiv_source_uses_returned_urls_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            result_path = Path(tmp) / "result.jpg"
            downloader = ImageDownloader("https://proxy.example.com")
            calls = []

            async def fake_download(url, timeout):
                calls.append(url)
                result_path.write_bytes(b"ok")
                return str(result_path)

            downloader.download = fake_download  # type: ignore[method-assign]
            await downloader.download_for_send(
                self._proxy_illust(source="pixiv"),
                quality="original",
                timeout=5,
                downgrade_limit_bytes=0,
                log_context="test",
            )

            self.assertEqual(
                calls, ["https://i.pximg.net/img-original/a.jpg?x=1"]
            )


class ImageDownloaderCleanupRegressionTest(unittest.IsolatedAsyncioTestCase):
    async def test_getsize_failure_cleans_downloaded_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "downloaded.jpg"
            downloader = ImageDownloader()

            async def fake_download(url, timeout):
                path.write_bytes(b"ok")
                return str(path)

            downloader.download = fake_download  # type: ignore[method-assign]
            with mock.patch.object(dl.os.path, "getsize", side_effect=OSError("secret")):
                with self.assertRaises(RuntimeError):
                    await downloader.download_for_send(
                        _illust_urls(),
                        quality="original",
                        timeout=5,
                        downgrade_limit_bytes=0,
                        log_context="test",
                    )

            self.assertFalse(path.exists())

    async def test_fdopen_failure_closes_descriptor_before_cleanup(self):
        created_paths = []

        def fake_mkstemp(prefix, suffix):
            path = Path(tmp) / f"{prefix}fdopen{suffix}"
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            created_paths.append(path)
            return fd, str(path)

        with tempfile.TemporaryDirectory() as tmp:
            resp = _FakeResponse(chunks=[_image_bytes()])
            downloader = _downloader_with(resp)
            with (
                mock.patch.object(dl.tempfile, "mkstemp", fake_mkstemp),
                mock.patch.object(dl.os, "fdopen", side_effect=OSError("failed")),
            ):
                with self.assertRaises(OSError):
                    await downloader.download("http://x/a.jpg", timeout=5)

            self.assertTrue(created_paths)
            self.assertFalse(any(path.exists() for path in created_paths))


if __name__ == "__main__":
    unittest.main()
