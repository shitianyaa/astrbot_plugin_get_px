import asyncio
import sys
import unittest
import tempfile
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.pixiv.filters import FiltersMixin
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.safety import ContentSafetyPolicy

@pytest.mark.asyncio
async def test_global_mode_reads_global_and_ignores_independent(tmp_path):
    class Spy:
        async def get_custom_safety_terms(self):
            return {"global"}
        async def get_blacklisted_illust_ids(self):
            return {"9"}
    m=object.__new__(FiltersMixin)
    m.image_index=Spy()
    p=ContentSafetyPolicy(builtin_terms_enabled=True,custom_terms=("local",),blacklisted_illust_ids=("8",))
    assert await m._blocked_query_term("global",p)

@pytest.mark.asyncio
async def test_independent_mode_avoids_global_reads():
    class Spy:
        async def get_custom_safety_terms(self):
            raise AssertionError
        async def get_blacklisted_illust_ids(self):
            raise AssertionError
        async def is_blacklisted(self,_):
            raise AssertionError
    m=object.__new__(FiltersMixin)
    m.image_index=Spy()
    p=ContentSafetyPolicy(builtin_terms_enabled=False,custom_terms=("local",),blacklisted_illust_ids=("8",))
    assert await m._blocked_query_term("local",p)

@pytest.mark.asyncio
async def test_independent_id_and_pid_filtering():
    m=object.__new__(FiltersMixin)
    m.image_index=None
    p=ContentSafetyPolicy(builtin_terms_enabled=False,blacklisted_illust_ids=("8",))
    out=await m._filter_blacklisted_illusts([{"id":"8"},{"pid":"8"},{"id":"1"}],p)
    assert len(out)==1

@pytest.mark.asyncio
async def test_blacklist_reads_global_only_in_on_mode():
    class Spy:
        def __init__(self):
            self.calls = []
        async def get_blacklisted_illust_ids(self):
            self.calls.append("ids")
            return {"9"}
        async def get_custom_safety_terms(self):
            self.calls.append("terms")
            return set()
    spy = Spy()
    mixin = object.__new__(FiltersMixin)
    mixin.image_index = spy
    items = [{"id": "9", "x_restrict": 0, "title": "safe", "tags": []}, {"id": "8", "x_restrict": 0, "title": "safe", "tags": []}]
    assert [x["id"] for x in await mixin._filter_blacklisted_illusts(items, ContentSafetyPolicy(builtin_terms_enabled=True, blacklisted_illust_ids=("8",)))] == ["8"]
    assert spy.calls == ["terms", "ids"]
    spy.calls.clear()
    assert [x["id"] for x in await mixin._filter_blacklisted_illusts(items, ContentSafetyPolicy(builtin_terms_enabled=False, blacklisted_illust_ids=("8",)))] == ["9"]
    assert spy.calls == []

def test_content_safety_policy_copies_mutable_inputs():
    terms, ids = ["alpha"], ["1"]
    policy = ContentSafetyPolicy(custom_terms=terms, blacklisted_illust_ids=ids)
    terms.append("beta")
    ids.append("2")
    assert policy.custom_terms == ("alpha",) and policy.blacklisted_illust_ids == ("1",)


def test_content_safety_policy_keeps_punctuation_variants_in_snapshot():
    policy = ContentSafetyPolicy(custom_terms=("r18g", "r-18g"))
    assert policy.custom_terms == ("r-18g", "r18g")


class FilteringTest(unittest.TestCase):
    def test_filter_manga_removes_manga_from_mixed_results(self):
        illusts = [
            {"id": "1", "type": "manga"},
            {"id": "2", "type": "illust"},
            {"id": "3", "type": "ugoira"},
        ]

        filtered = FiltersMixin._filter_manga(illusts)

        self.assertEqual([illust["id"] for illust in filtered], ["2", "3"])

    def test_safe_rating_only_keeps_general_audience(self):
        filtered = FiltersMixin._filter_safe_rating(
            [
                {"id": "1", "x_restrict": 0},
                {"id": "2", "x_restrict": 1},
                {"id": "3", "x_restrict": 2},
            ]
        )
        self.assertEqual([item["id"] for item in filtered], ["1"])


class SafetyFilteringTest(unittest.IsolatedAsyncioTestCase):
    async def test_all_four_switch_combinations_apply_independently(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixin = object.__new__(FiltersMixin)
            mixin.image_index = ImageIndexStore(tmp)
            try:
                candidates = [
                    {"id": "safe", "x_restrict": 0, "title": "safe", "tags": []},
                    {"id": "rated", "x_restrict": 1, "title": "safe", "tags": []},
                    {"id": "builtin", "x_restrict": 0, "title": "guro", "tags": []},
                ]
                expected = {
                    (True, True): ["safe"],
                    (True, False): ["safe", "builtin"],
                    (False, True): ["safe", "rated"],
                    (False, False): ["safe", "rated", "builtin"],
                }
                for switches, ids in expected.items():
                    with self.subTest(switches=switches):
                        result = await mixin._filter_blacklisted_illusts(
                            candidates, ContentSafetyPolicy(*switches)
                        )
                        self.assertEqual([item["id"] for item in result], ids)
            finally:
                mixin.image_index.close()

    async def test_independent_lists_replace_global_terms_and_id_blacklist(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixin = object.__new__(FiltersMixin)
            mixin.image_index = ImageIndexStore(tmp)
            try:
                policy = ContentSafetyPolicy(
                    general_only_enabled=False,
                    builtin_terms_enabled=False,
                    custom_terms=("customblocked",),
                    blacklisted_illust_ids=("99",),
                )
                self.assertFalse(await mixin._blocked_query_term("guro", policy))
                self.assertTrue(
                    await mixin._blocked_query_term("customblocked", policy)
                )
                filtered = await mixin._filter_blacklisted_illusts(
                    [
                        {"id": "1", "x_restrict": 1, "title": "guro", "tags": []},
                        {"id": "2", "x_restrict": 1, "title": "customblocked", "tags": []},
                        {"id": "99", "x_restrict": 0, "title": "safe", "tags": []},
                    ],
                    policy,
                )
                self.assertEqual([item["id"] for item in filtered], ["1"])
            finally:
                mixin.image_index.close()

    async def test_private_and_policy_read_failures_use_strict_defaults(self):
        class Event:
            def __init__(self, group_id):
                self.group_id = group_id

            def get_group_id(self):
                return self.group_id

        class BrokenStore:
            async def get_group_content_safety(self, group_id):
                raise OSError(group_id)

        mixin = object.__new__(FiltersMixin)
        mixin.checkin_store = BrokenStore()
        self.assertEqual(
            await mixin._content_safety_policy(Event("")),
            ContentSafetyPolicy(),
        )
        self.assertEqual(
            await mixin._content_safety_policy(Event("group-a")),
            ContentSafetyPolicy(),
        )

    async def test_builtin_and_custom_terms_filter_queries_and_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixin = object.__new__(FiltersMixin)
            mixin.image_index = ImageIndexStore(tmp)
            try:
                self.assertTrue(await mixin._blocked_query_term("g u r o illustration"))
                await mixin.image_index.add_safety_term("危险主题", added_by="test")
                self.assertTrue(await mixin._blocked_query_term("危险-主题 壁纸"))
                filtered = await mixin._filter_blacklisted_illusts(
                    [
                        {"id": "1", "x_restrict": 0, "title": "safe", "tags": []},
                        {"id": "2", "x_restrict": 0, "title": "危险主题", "tags": []},
                        {"id": "3", "x_restrict": 1, "title": "safe", "tags": []},
                    ]
                )
                self.assertEqual([item["id"] for item in filtered], ["1"])
            finally:
                mixin.image_index.close()

    async def test_blacklist_store_failure_is_fail_closed(self):
        class BrokenIndex:
            async def get_custom_safety_terms(self):
                return set()

            async def get_blacklisted_illust_ids(self):
                raise OSError("database unavailable")

        mixin = object.__new__(FiltersMixin)
        mixin.image_index = BrokenIndex()
        with self.assertRaisesRegex(RuntimeError, "内容安全服务暂不可用"):
            await mixin._filter_blacklisted_illusts(
                [{"id": "1", "x_restrict": 0, "title": "safe", "tags": []}]
            )

    async def test_lolicon_page_id_is_blocked_by_pixiv_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            mixin = object.__new__(FiltersMixin)
            mixin.image_index = ImageIndexStore(tmp)
            try:
                await mixin.image_index.add_blacklist_illust(illust_id="123")

                filtered = await mixin._filter_blacklisted_illusts(
                    [
                        {
                            "id": "123:1",
                            "pid": "123",
                            "x_restrict": 0,
                            "title": "safe",
                            "tags": [],
                        }
                    ]
                )

                self.assertEqual(filtered, [])
            finally:
                mixin.image_index.close()


if __name__ == "__main__":
    unittest.main()

def test_content_safety_cache_identity_separates_private_and_group():
    from pixiv.safety import ContentSafetyPolicy
    assert ContentSafetyPolicy(user_id="1").cache_identity() != ContentSafetyPolicy(group_id="1").cache_identity()
    assert ContentSafetyPolicy(user_id="1").cache_identity() != ContentSafetyPolicy(user_id="2").cache_identity()


class PolicyEvent:
    def __init__(self, group_id="", sender_id=""):
        self.group_id = group_id
        self.sender_id = sender_id

    def get_group_id(self):
        return self.group_id

    def get_sender_id(self):
        return self.sender_id


class RecordingPolicyService:
    def __init__(self, *, fail_group=False, fail_private=False):
        self.calls = []
        self.fail_group = fail_group
        self.fail_private = fail_private

    async def get_group_policy(self, group_id):
        self.calls.append(("group", group_id))
        if self.fail_group:
            raise RuntimeError("group failed")
        return {"general_only_enabled": False, "builtin_terms_enabled": True}

    async def get_private_policy(self, user_id):
        self.calls.append(("private", user_id))
        if self.fail_private:
            raise RuntimeError("private failed")
        return {"general_only_enabled": True, "builtin_terms_enabled": False}


async def _resolved_policy(event, service):
    mixin = object.__new__(FiltersMixin)
    mixin.group_safety_service = service
    return await mixin._content_safety_policy(event)


def test_runtime_policy_resolution_uses_exactly_one_scope():
    async def run():
        service = RecordingPolicyService()
        group_policy = await _resolved_policy(PolicyEvent("g1", "u1"), service)
        assert service.calls == [("group", "g1")]
        assert group_policy.group_id == "g1" and group_policy.user_id == ""

        service.calls.clear()
        private_policy = await _resolved_policy(PolicyEvent("", "u1"), service)
        assert service.calls == [("private", "u1")]
        assert private_policy.user_id == "u1" and private_policy.group_id == ""

    asyncio.run(run())


def test_runtime_policy_failures_are_strict_without_cross_scope_fallback():
    async def run():
        service = RecordingPolicyService(fail_group=True)
        policy = await _resolved_policy(PolicyEvent("g1", "u1"), service)
        assert policy == ContentSafetyPolicy()
        assert service.calls == [("group", "g1")]

        service = RecordingPolicyService(fail_private=True)
        policy = await _resolved_policy(PolicyEvent("", "u1"), service)
        assert policy == ContentSafetyPolicy()
        assert service.calls == [("private", "u1")]

    asyncio.run(run())
