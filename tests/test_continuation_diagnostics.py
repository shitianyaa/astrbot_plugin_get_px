"""Continuation diagnostics expose categories, never URLs or query values."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from test_image_selection import client_with_response
from test_p_coin_cost import _FlowEvent
from astrbot_plugin_get_px.pixiv.pagination import (
    SourceResponseError,
    parse_continuation,
)
from astrbot_plugin_get_px.pixiv.search import SearchMixin

BASE = "https://app-api.pixiv.net/v1/illust/recommended?"


@pytest.mark.parametrize(
    "value, detail, field",
    [
        (123, "value_type", "none"),
        ("https://[private-host/v1/illust/recommended?offset=1", "url_syntax", "none"),
        (
            "https://user-secret@example.org/v1/illust/recommended?offset=1",
            "host",
            "none",
        ),
        ("http://app-api.pixiv.net/v1/illust/recommended?offset=1", "scheme", "none"),
        ("https://app-api.pixiv.net/v1/search/illust?offset=1", "path", "none"),
        (BASE + "offset=1#private-fragment", "fragment", "none"),
        (
            BASE + "offset=1&private-token-name=private-value",
            "unknown_parameter",
            "unknown",
        ),
        (BASE + "offset=1&offset=2", "scalar_count", "offset"),
        (BASE + "offset=-12345", "number", "offset"),
        (
            BASE + "min_bookmark_id_for_recent_illust=0",
            "number",
            "min_bookmark_id_for_recent_illust",
        ),
        (
            BASE + "include_privacy_policy=PrivateBool",
            "boolean",
            "include_privacy_policy",
        ),
        (BASE + "filter=PrivateFilter", "static_value", "filter"),
        (BASE + "viewed[52]=123", "list_indices", "viewed"),
        (BASE + "viewed[0]=1&viewed[00]=2", "list_index", "viewed"),
        (BASE + "viewed[]=1,2", "list_format", "viewed"),
        (
            BASE + "bookmark_illust_ids[]=private-id",
            "list_number",
            "bookmark_illust_ids",
        ),
        (BASE + "offset", "query_syntax", "none"),
        (BASE, "empty_parameters", "none"),
    ],
)
def test_rejection_has_only_fixed_diagnostic_metadata(value, detail, field):
    with pytest.raises(SourceResponseError) as caught:
        parse_continuation(value, recommended=True)
    error = caught.value
    assert error.reason == "invalid_continuation"
    assert error.detail == detail
    assert error.field == field
    assert str(error) == "图片源响应不可用: reason=invalid_continuation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected",
    [
        ("offset=-12345", "continuation_detail=number continuation_field=offset"),
        (
            "private-token-name=private-value",
            "continuation_detail=unknown_parameter continuation_field=unknown",
        ),
    ],
)
async def test_source_warning_is_redacted_and_fallback_is_unchanged(query, expected):
    source = object.__new__(SearchMixin)
    source._cfg_str = lambda key, default="": "Pixiv"
    source.client = client_with_response({"illusts": [], "next_url": BASE + query})
    source.image_index = None
    source.lolicon_client = SimpleNamespace(
        available=True, random=AsyncMock(return_value=[{"id": "fixture"}])
    )
    with patch("astrbot_plugin_get_px.pixiv.search.logger") as logger:
        items, _, key = await source._fetch_source_candidates(_FlowEvent(), "")
    assert key == "lolicon:random" and items == [{"id": "fixture"}]
    text = logger.warning.call_args.args[0]
    assert "reason=invalid_continuation" in text and expected in text
    for forbidden in ["private-token-name", "private-value", "-12345", BASE]:
        assert forbidden not in text


def test_search_static_diagnostic_does_not_expose_tag():
    with pytest.raises(SourceResponseError) as caught:
        parse_continuation(
            "https://app-api.pixiv.net/v1/search/illust?offset=1&word=private-search",
            recommended=False,
            tag="expected-private-search",
        )
    assert (caught.value.detail, caught.value.field) == ("static_value", "word")
    assert "private-search" not in str(caught.value)
