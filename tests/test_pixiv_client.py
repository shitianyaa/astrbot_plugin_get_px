import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.client import PixivClient  # noqa: E402


class _FakeNotFoundError(Exception):
    status = 404


class _FakeApi:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.closed = False

    async def illust_detail(self, _illust_id):
        if self.error is not None:
            raise self.error
        return self.result

    async def close(self):
        self.closed = True


def _logged_in_client(api) -> PixivClient:
    client = PixivClient("token")
    client._api = api
    client._cached_token = "token"
    client._expires_at = time.monotonic() + 300
    return client


class PixivClientIllustDetailTest(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_api_and_prevents_future_login(self):
        api = _FakeApi()
        client = _logged_in_client(api)

        await client.close()

        self.assertTrue(api.closed)
        self.assertIsNone(client.api)
        with self.assertRaisesRegex(RuntimeError, "已关闭"):
            await client.ensure_logged_in()

    async def test_close_waits_for_in_flight_api_request(self):
        request_started = asyncio.Event()
        release_request = asyncio.Event()

        class SlowApi(_FakeApi):
            async def illust_detail(self, _illust_id):
                request_started.set()
                await release_request.wait()
                return {"illust": {"id": 42}}

        api = SlowApi()
        client = _logged_in_client(api)
        request = asyncio.create_task(client.illust_detail(42))
        await request_started.wait()
        closing = asyncio.create_task(client.close())
        await asyncio.sleep(0)

        self.assertFalse(closing.done())
        self.assertFalse(api.closed)
        release_request.set()
        self.assertEqual(await request, {"id": 42})
        await closing
        self.assertTrue(api.closed)

    async def test_returns_none_when_response_has_no_illust(self):
        client = _logged_in_client(_FakeApi(result={"illust": None}))

        self.assertIsNone(await client.illust_detail(123))

    async def test_returns_none_for_explicit_not_found_error(self):
        client = _logged_in_client(_FakeApi(error=_FakeNotFoundError("not found")))

        self.assertIsNone(await client.illust_detail(123))

    async def test_reraises_non_not_found_api_error(self):
        client = _logged_in_client(_FakeApi(error=RuntimeError("token expired")))

        with self.assertRaises(RuntimeError):
            await client.illust_detail(123)


if __name__ == "__main__":
    unittest.main()
