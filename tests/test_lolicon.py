import unittest

from astrbot_plugin_get_px.pixiv.lolicon import LoliconClient
from astrbot_plugin_get_px.pixiv.search import SearchMixin


class _FakeResponse:
    status = 200

    def __init__(self):
        self.payload = {
            "data": [
                {
                    "pid": 123,
                    "p": 1,
                    "uid": 456,
                    "title": "sample",
                    "author": "artist",
                    "r18": False,
                    "width": 750,
                    "height": 1000,
                    "tags": ["初音ミク"],
                    "aiType": 1,
                    "urls": {
                        "original": "https://img/original.jpg",
                        "regular": "https://img/regular.jpg",
                        "small": "https://img/small.jpg",
                        "thumb": "https://img/thumb.jpg",
                        "mini": "https://img/mini.jpg",
                    },
                }
            ]
        }

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, **_kwargs):
        return self.payload


class _FakeSession:
    closed = False

    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeResponse()


class _SourceEvent:
    def get_group_id(self):
        return "1"


class _SourceLolicon:
    available = True

    def __init__(self, *, error=False):
        self.error = error
        self.calls = []

    async def search(self, tag, **kwargs):
        self.calls.append(("search", tag, kwargs))
        if self.error:
            raise RuntimeError("down")
        return [{"id": "1"}]

    async def random(self, **kwargs):
        self.calls.append(("random", kwargs))
        if self.error:
            raise RuntimeError("down")
        return [{"id": "2"}]


class _SourcePixiv:
    def __init__(self):
        self.search_calls = []
        self.recommended_calls = []

    async def search(self, tag, offset=0):
        self.search_calls.append((tag, offset))
        return [{"id": "3"}]

    async def recommended(self, offset=0):
        self.recommended_calls.append(offset)
        return [{"id": "4"}]


class _SourceHarness(SearchMixin):
    def __init__(self, lolicon, pixiv=None):
        self.lolicon_client = lolicon
        self.client = pixiv
        self.image_index = None

    def _init_client(self):
        return None


class LoliconClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_request_and_normalization_keep_all_sizes(self):
        client = LoliconClient()
        session = _FakeSession()
        client._session = session

        results = await client.search("初音ミク", count=3, aspect_ratio="vertical")

        self.assertEqual(results[0]["id"], "123:1")
        self.assertEqual(results[0]["page"], 1)
        self.assertEqual(results[0]["user"]["name"], "artist")
        self.assertEqual(results[0]["image_urls"]["large"], "https://img/regular.jpg")
        self.assertEqual(
            results[0]["meta_single_page"]["original_image_url"],
            "https://img/original.jpg",
        )
        params = session.calls[0][1]["params"]
        self.assertIn(("r18", "0"), params)
        self.assertIn(("excludeAI", "true"), params)
        self.assertIn(("tag", "初音ミク"), params)
        self.assertIn(("aspectRatio", "vertical"), params)
        self.assertEqual([value for key, value in params if key == "size"], [
            "original", "regular", "small", "thumb", "mini"
        ])

    async def test_tagged_lolicon_success_does_not_call_pixiv(self):
        lolicon = _SourceLolicon()
        pixiv = _SourcePixiv()
        plugin = _SourceHarness(lolicon, pixiv)

        illusts, _, source = await plugin._fetch_source_candidates(
            _SourceEvent(), "初音ミク"
        )

        self.assertEqual(illusts, [{"id": "1"}])
        self.assertEqual(source, "lolicon:search:初音ミク")
        self.assertEqual(pixiv.search_calls, [])
        self.assertEqual(pixiv.recommended_calls, [])

    async def test_untagged_lolicon_failure_uses_pixiv_recommended(self):
        pixiv = _SourcePixiv()
        plugin = _SourceHarness(_SourceLolicon(error=True), pixiv)

        illusts, _, source = await plugin._fetch_source_candidates(
            _SourceEvent(), ""
        )

        self.assertEqual(illusts, [{"id": "4"}])
        self.assertEqual(source, "pixiv:recommended")
        self.assertEqual(pixiv.search_calls, [])
        self.assertEqual(pixiv.recommended_calls, [0])


if __name__ == "__main__":
    unittest.main()
