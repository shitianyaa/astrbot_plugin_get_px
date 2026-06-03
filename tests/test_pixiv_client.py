import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv_client import PixivClient  # noqa: E402


class _FakeNotFoundError(Exception):
    status = 404


class _FakeApi:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    async def illust_detail(self, _illust_id):
        if self.error is not None:
            raise self.error
        return self.result


def _logged_in_client(api) -> PixivClient:
    client = PixivClient("token")
    client._api = api
    client._cached_token = "token"
    client._expires_at = time.monotonic() + 300
    return client


class PixivClientIllustDetailTest(unittest.IsolatedAsyncioTestCase):
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
