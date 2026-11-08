import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.lolicon import DEFAULT_API_URL, LoliconClient
from astrbot_plugin_get_px.pixiv.pagination import PixivPage
from astrbot_plugin_get_px.pixiv.constants import AIOCQHTTP_PLATFORM
from astrbot_plugin_get_px.pixiv.search import SearchMixin
from astrbot_plugin_get_px.pixiv.safety import ContentSafetyPolicy


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

    def __init__(self, *, error=False, empty=False, order=None):
        self.error = error
        self.empty = empty
        self.order = order if order is not None else []
        self.calls = []

    async def search(self, tag, **kwargs):
        self.calls.append(("search", tag, kwargs))
        self.order.append("lolicon")
        if self.error:
            raise RuntimeError("down")
        return [] if self.empty else [{"id": "1"}]

    async def random(self, **kwargs):
        self.calls.append(("random", kwargs))
        self.order.append("lolicon")
        if self.error:
            raise RuntimeError("down")
        return [] if self.empty else [{"id": "2"}]


class _SourcePixiv:
    def __init__(self, *, error=False, empty=False, order=None):
        self.error = error
        self.empty = empty
        self.order = order if order is not None else []
        self.search_calls = []
        self.recommended_calls = []

    async def search(self, tag, offset=0, *, continuation=None, portrait=False):
        self.search_calls.append((tag, offset))
        self.order.append("pixiv")
        if self.error:
            raise RuntimeError("down")
        return PixivPage([] if self.empty else [{"id": "3"}], None)

    async def recommended(self, offset=0, *, continuation=None):
        self.recommended_calls.append(offset)
        self.order.append("pixiv")
        if self.error:
            raise RuntimeError("down")
        return PixivPage([] if self.empty else [{"id": "4"}], None)


class _SourceHarness(SearchMixin):
    def __init__(self, lolicon, pixiv=None, *, preferred="Lolicon"):
        self.lolicon_client = lolicon
        self.client = pixiv
        self.preferred = preferred
        self.image_index = None

    def _cfg_str(self, key, default=""):
        return self.preferred if key == "preferred_image_source" else default

    def _init_client(self):
        return None


class LoliconClientTest(unittest.IsolatedAsyncioTestCase):
    def test_forward_threshold_uses_successful_download_count_and_platform(self):
        self.assertTrue(
            SearchMixin._should_use_forward(
                downloaded_count=1, threshold=0, platform_name=AIOCQHTTP_PLATFORM
            )
        )
        self.assertTrue(
            SearchMixin._should_use_forward(
                downloaded_count=2, threshold=1, platform_name=AIOCQHTTP_PLATFORM
            )
        )
        self.assertFalse(
            SearchMixin._should_use_forward(
                downloaded_count=1, threshold=1, platform_name=AIOCQHTTP_PLATFORM
            )
        )
        self.assertFalse(
            SearchMixin._should_use_forward(
                downloaded_count=2, threshold=1, platform_name="qq_official"
            )
        )

    async def test_request_and_normalization_keep_all_sizes(self):
        client = LoliconClient()
        session = _FakeSession()
        client._session = session

        results = await client.search(
            "初音ミク", count=3, aspect_ratio="gt0.60lt0.90"
        )

        self.assertEqual(results[0]["id"], "123:1")
        self.assertEqual(results[0]["page"], 1)
        self.assertEqual(results[0]["user"]["name"], "artist")
        self.assertEqual(results[0]["image_urls"]["large"], "https://img/regular.jpg")
        self.assertEqual(
            results[0]["meta_single_page"]["original_image_url"],
            "https://img/original.jpg",
        )
        self.assertEqual(session.calls[0][0], DEFAULT_API_URL)
        params = session.calls[0][1]["params"]
        self.assertIn(("r18", "0"), params)
        self.assertIn(("excludeAI", "true"), params)
        self.assertIn(("tag", "初音ミク"), params)
        self.assertIn(("aspectRatio", "gt0.60lt0.90"), params)
        self.assertEqual([value for key, value in params if key == "size"], [
            "original", "regular", "small", "thumb", "mini"
        ])

    async def test_r18_mode_accepts_only_strict_or_mixed(self):
        client = LoliconClient()
        session = _FakeSession()
        client._session = session
        await client.random(r18=2)
        self.assertIn(("r18", "2"), session.calls[0][1]["params"])
        with self.assertRaises(ValueError):
            await client.random(r18=1)

    async def test_group_policy_disables_upstream_general_only_filter(self):
        lolicon = _SourceLolicon()
        plugin = _SourceHarness(lolicon)
        await plugin._fetch_source_candidates(
            _SourceEvent(),
            "",
            policy=ContentSafetyPolicy(general_only_enabled=False),
        )
        self.assertEqual(lolicon.calls[0][1]["r18"], 2)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("preferred", ["Lolicon", "Pixiv"])
@pytest.mark.parametrize("outcome", ["success", "empty", "error"])
async def test_preferred_source_success_or_fallback(preferred, outcome):
    tag = "初音ミク"
    order = []
    first = preferred.lower()
    other = "pixiv" if first == "lolicon" else "lolicon"
    options = {"empty": outcome == "empty", "error": outcome == "error"}
    lolicon = _SourceLolicon(order=order, **(options if first == "lolicon" else {}))
    pixiv = _SourcePixiv(order=order, **(options if first == "pixiv" else {}))
    plugin = _SourceHarness(lolicon, pixiv, preferred=preferred)

    illusts, raw_count, source = await plugin._fetch_source_candidates(_SourceEvent(), tag)

    selected = first if outcome == "success" else other
    assert order == ([first] if outcome == "success" else [first, other])
    assert source == f"{selected}:search:{tag}"
    assert illusts == [{"id": "1" if selected == "lolicon" else "3"}]
    assert raw_count == 1


@pytest.mark.asyncio
async def test_both_sources_unavailable_returns_no_candidates():
    order = []
    plugin = _SourceHarness(
        _SourceLolicon(empty=True, order=order), _SourcePixiv(error=True, order=order),
    )
    illusts, count, _ = await plugin._fetch_source_candidates(_SourceEvent(), "")
    assert illusts == [] and count == 0
    assert order == ["lolicon", "pixiv"]


@pytest.mark.asyncio
async def test_missing_preferred_pixiv_client_uses_lolicon():
    lolicon = _SourceLolicon()
    plugin = _SourceHarness(lolicon, preferred="Pixiv")
    illusts, count, source = await plugin._fetch_source_candidates(_SourceEvent(), "")
    assert illusts and count == 1 and source.startswith("lolicon:")
    assert len(lolicon.calls) == 1


@pytest.mark.asyncio
async def test_invalid_preferred_source_uses_default_lolicon():
    pixiv = _SourcePixiv()
    plugin = _SourceHarness(_SourceLolicon(), pixiv, preferred="invalid")
    _, _, source = await plugin._fetch_source_candidates(_SourceEvent(), "")
    assert source == "lolicon:random"
    assert pixiv.recommended_calls == []


@pytest.mark.asyncio
async def test_preferred_pixiv_random_uses_recommended_page_cursor():
    pixiv = _SourcePixiv()
    lolicon = _SourceLolicon()
    plugin = _SourceHarness(lolicon, pixiv, preferred="Pixiv")
    index = SimpleNamespace(get_page_offset=AsyncMock(return_value=60))
    plugin.image_index = index
    _, _, source = await plugin._fetch_source_candidates(_SourceEvent(), "")
    assert source == "pixiv:recommended"
    assert pixiv.recommended_calls == [60]
    assert lolicon.calls == []
    index.get_page_offset.assert_awaited_once_with("group:1", source)


@pytest.mark.asyncio
async def test_pixiv_fallback_to_lolicon_keeps_safety_and_aspect_params():
    lolicon = _SourceLolicon()
    plugin = _SourceHarness(lolicon, _SourcePixiv(error=True), preferred="Pixiv")
    await plugin._fetch_source_candidates(
        _SourceEvent(), "初音ミク", count=3, aspect_ratio="gt0.60lt0.90",
        policy=ContentSafetyPolicy(general_only_enabled=False),
    )
    assert lolicon.calls == [("search", "初音ミク", {
        "count": 3, "aspect_ratio": "gt0.60lt0.90", "r18": 2,
    })]


if __name__ == "__main__":
    unittest.main()
