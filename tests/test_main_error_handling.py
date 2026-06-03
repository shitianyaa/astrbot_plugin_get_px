import json
import sys
import unittest
from pathlib import Path

from quart import Quart

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402


class _FakeEvent:
    def get_sender_id(self):
        return "10001"

    def plain_result(self, text):
        return text


class _FailingClient:
    async def illust_detail(self, _illust_id):
        raise RuntimeError(r"token expired at C:\secret\pixiv.db")


async def _collect(async_iterable):
    return [item async for item in async_iterable]


class MainErrorHandlingTest(unittest.IsolatedAsyncioTestCase):
    async def test_handle_info_hides_pixiv_detail_exception(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.client = _FailingClient()

        results = await _collect(plugin._handle_info(_FakeEvent(), 123456))

        self.assertEqual(results, ["❌ 获取作品详情失败，请稍后再试"])

    async def test_handle_download_hides_pixiv_detail_exception(self):
        plugin = object.__new__(GetPxPlugin)
        plugin.client = _FailingClient()
        plugin._check_rate_limit = lambda _sender_id: 0

        results = await _collect(plugin._handle_download(_FakeEvent(), 123456))

        self.assertEqual(results, ["❌ 获取作品详情失败，请稍后再试"])

    async def test_web_internal_error_response_is_sanitized(self):
        plugin = object.__new__(GetPxPlugin)
        app = Quart(__name__)

        async with app.app_context():
            response, status = plugin._web_internal_error(
                "test", RuntimeError(r"C:\secret\pixiv.db is locked")
            )
            body = await response.get_data(as_text=True)

        payload = json.loads(body)
        self.assertEqual(status, 500)
        self.assertEqual(payload, {"success": False, "error": "服务内部错误，请稍后重试"})
        self.assertNotIn("pixiv.db", body)
        self.assertNotIn("secret", body)


if __name__ == "__main__":
    unittest.main()
