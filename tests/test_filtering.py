from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pixiv.filters import FiltersMixin
from pixiv.safety import ContentSafetyPolicy, matched_illustration_term


def test_tag_matching_is_exact_and_strict_adds_title_not_caption():
    loose = ContentSafetyPolicy(custom_terms=("猫耳", "blue archive"))
    strict = ContentSafetyPolicy(match_mode="严格", custom_terms=loose.custom_terms)
    assert matched_illustration_term({"tags": [{"name": "ＢLUE ARCHIVE"}]}, loose)
    assert matched_illustration_term({"tags": [{"translated_name": "猫耳"}]}, loose)
    assert not matched_illustration_term(
        {"tags": [{"name": "猫耳少女"}], "caption": "猫耳"}, loose
    )
    assert not matched_illustration_term({"title": "猫耳少女"}, loose)
    assert matched_illustration_term({"title": "猫耳少女"}, strict)
    assert not matched_illustration_term({"caption": "猫耳"}, strict)
    # 内置词完全不存在，分级正常的作品不因旧词表自动被拦截。
    assert not matched_illustration_term(
        {"title": "成人礼", "tags": [{"name": "guro"}]}, ContentSafetyPolicy()
    )


@pytest.mark.asyncio
async def test_rating_keywords_and_pid_blacklist_apply_to_same_snapshot():
    mixin = object.__new__(FiltersMixin)
    mixin.image_index = None
    items = [
        {"id": "1", "x_restrict": 0},
        {"id": "2", "x_restrict": 1},
        {"id": "3", "x_restrict": 0, "tags": [{"name": "blocked"}]},
        {"id": "8:0", "pid": "8", "x_restrict": 0},
        {"id": "8:1", "x_restrict": 0},
    ]
    for general_only, expected in ((True, ["1"]), (False, ["1", "2"])):
        policy = ContentSafetyPolicy(
            general_only_enabled=general_only,
            custom_terms=("blocked",),
            blacklisted_illust_ids=("8",),
        )
        assert [
            item["id"]
            for item in await mixin._filter_blacklisted_illusts(items, policy)
        ] == expected
        assert await mixin._blacklist_reason_for_illust(items[-1], policy=policy)


@pytest.mark.asyncio
async def test_policy_resolution_uses_full_origin_and_fails_closed():
    mixin = object.__new__(FiltersMixin)
    policy = ContentSafetyPolicy(custom_terms=("alpha",))
    mixin.safety_service = SimpleNamespace(
        resolve=AsyncMock(return_value=policy),
        global_policy=AsyncMock(return_value=policy),
    )
    assert (
        await mixin._content_safety_policy(
            SimpleNamespace(unified_msg_origin="bot:GroupMessage:1")
        )
        == policy
    )
    mixin.safety_service.resolve.assert_awaited_once_with("bot:GroupMessage:1")
    assert await mixin._content_safety_policy(None) == policy
    mixin.safety_service.resolve.side_effect = OSError("secret")
    with pytest.raises(RuntimeError, match="内容安全服务暂不可用"):
        await mixin._content_safety_policy(
            SimpleNamespace(unified_msg_origin="bot:GroupMessage:1")
        )


def test_snapshot_copies_lists_and_cache_identity_tracks_only_effective_rules():
    terms, ids = ["alpha"], ["001"]
    policy = ContentSafetyPolicy(custom_terms=terms, blacklisted_illust_ids=ids)
    terms.append("beta")
    ids.append("2")
    assert policy.custom_terms == ("alpha",) and policy.blacklisted_illust_ids == ("1",)
    equivalent = ContentSafetyPolicy(
        custom_terms=("ＡLPHA",), blacklisted_illust_ids=("1",)
    )
    assert policy.cache_identity() == equivalent.cache_identity()
    assert (
        policy.cache_identity()
        != ContentSafetyPolicy(
            match_mode="严格", custom_terms=("alpha",), blacklisted_illust_ids=("1",)
        ).cache_identity()
    )


def test_manga_filter():
    items = [
        {"id": "1", "type": "manga", "x_restrict": 0},
        {"id": "2", "type": "illust", "x_restrict": 1},
    ]
    assert FiltersMixin._filter_manga(items) == [items[1]]
