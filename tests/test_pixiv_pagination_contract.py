"""真实插件、SDK和本地HTTP贯通；只替换远端路由与QQ平台回执。"""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlencode, urlsplit
from urllib.request import url2pathname

import aiohttp
from aiohttp import web
from PIL import Image
from pixivpy_async import AppPixivAPI
import pytest
import pytest_asyncio
from astrbot.api import AstrBotConfig

from test_image_tools import Event
from astrbot_plugin_get_px.main import GetPxPlugin
from astrbot_plugin_get_px.checkin import CheckinStore
from astrbot_plugin_get_px.group_safety import SessionSafetyService
from astrbot_plugin_get_px.pixiv.client import PixivClient
from astrbot_plugin_get_px.pixiv.downloader import ImageDownloader, cleanup
from astrbot_plugin_get_px.pixiv.index import ImageIndexStore
from astrbot_plugin_get_px.pixiv.query import ArtworkQuery, QueryError
from astrbot_plugin_get_px.tools.service import ImageToolService


RECOMMENDED = "/v1/illust/recommended"
CURSOR = {
    "offset": 30,
    "max_bookmark_id_for_recommend": "null",
    "min_bookmark_id_for_recent_illust": 123,
    "viewed": [11, 12],
    "bookmark_illust_ids": [21, 22],
    "include_ranking_label": False,
    "include_ranking_illusts": False,
    "include_privacy_policy": True,
}


def next_url(path, params):
    return "https://app-api.pixiv.net" + path + "?" + urlencode(params)


def recommended_params(token="null"):
    return [
        ("content_type", "illust"), ("filter", "for_ios"), ("offset", "30"),
        ("max_bookmark_id_for_recommend", token), ("min_bookmark_id_for_recent_illust", "123"),
        ("viewed[0]", "11"), ("viewed[1]", "12"),
        ("bookmark_illust_ids[]", "21,22"),
        ("include_ranking_label", "false"), ("include_ranking_illusts", "false"),
        ("include_privacy_policy", "true"),
    ]


def event_context():
    event = Event()
    event.get_group_id = lambda: "1"
    event.stop_event = Mock()
    async def acknowledge(action, **params):
        assert action == "send_group_msg" and params["group_id"] == 1
        for segment in params["message"]:
            if segment["type"] == "image":
                location = urlsplit(segment["data"]["file"])
                assert location.scheme == "file"
                with Image.open(url2pathname(location.path)) as image:
                    image.verify()
        return {"message_id": 7}

    event.bot.call_action = AsyncMock(side_effect=acknowledge)
    return SimpleNamespace(context=SimpleNamespace(event=event), tool_call_timeout=120)


@pytest_asyncio.fixture
async def runtime(tmp_path):
    image = BytesIO()
    Image.new("RGB", (75, 100), "blue").save(image, format="PNG")
    state = SimpleNamespace(pages=[], calls=[], image_requests=[])

    async def serve(request):
        if request.path.startswith("/image/"):
            state.image_requests.append(request.path)
            return web.Response(body=image.getvalue(), content_type="image/png")
        assert request.headers["Authorization"] == "Bearer fixture-access"
        state.calls.append((request.path, dict(request.query)))
        assert state.pages, "插件发出了预期之外的上游请求"
        return web.json_response(state.pages.pop(0))

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    state.origin = str(runner.addresses[0][0]) + ":" + str(runner.addresses[0][1])
    state.origin = "http://" + state.origin
    session = aiohttp.ClientSession(trust_env=False)
    sdk = AppPixivAPI()
    sdk.access_token = "fixture-access"

    async def redirected_request(method, url, headers, params, data):
        target = urlsplit(url)
        assert target.scheme == "https" and target.netloc == "app-api.pixiv.net"
        async with session.request(method, state.origin + target.path, headers=headers, params=params, data=data) as response:
            response.raise_for_status()
            return await response.json()

    sdk.req = redirected_request
    client = PixivClient("fixture-refresh")
    client._api, client._cached_token, client._expires_at = sdk, "fixture-refresh", float("inf")
    schema = json.loads((Path(__file__).resolve().parents[1] / "_conf_schema.json").read_text(encoding="utf-8"))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = AstrBotConfig(config_path=str(tmp_path / "plugin.json"), schema=deepcopy(schema))
    plugin.config["pixiv_source"].update(preferred_image_source="Pixiv", pixiv_refresh_token="fixture-refresh")
    plugin.config["runtime"].update(rate_limit_seconds=0)
    plugin.config["checkin_basic"].update(checkin_background_tag="")
    plugin.config.save_config()
    plugin._last_request = {}
    plugin.client = client
    plugin.lolicon_client = SimpleNamespace(available=True, random=AsyncMock(side_effect=AssertionError("不应回退Lolicon")))
    plugin.downloader = ImageDownloader(temp_dir=tmp_path / "images")
    plugin.image_index = ImageIndexStore(tmp_path)
    plugin.checkin_store = CheckinStore(tmp_path)
    await plugin.checkin_store.get_profile("10001")
    await plugin.checkin_store.update_checkin_member(user_id="10001", coins=100, affection=0, total_days=0, streak_days=0)
    plugin.safety_service = SessionSafetyService(plugin.config)
    await plugin.safety_service.initialize(plugin.checkin_store, plugin.image_index, tmp_path)
    state.plugin, state.client, state.directory = plugin, client, tmp_path

    def artwork(identity, **values):
        return dict(id=identity, title="fixture", type="illust", x_restrict=0,
                    width=75, height=100, tags=[], user={"id": 42, "name": "artist"},
                    meta_single_page={"original_image_url": state.origin + f"/image/{identity}.png"}, **values)

    state.artwork = artwork
    try:
        yield state
    finally:
        await plugin.downloader.close()
        plugin.image_index.close()
        await client.close()
        await session.close()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["command", "model_auto", "checkin"])
async def test_recommendation_survives_plugin_selection_delivery_and_database_reopen(runtime, entry):
    state, plugin = runtime, runtime.plugin
    # 真实18项响应：17项按默认安全规则排除，本批唯一可用图成功后应保存下一位置。
    blocked = [dict(state.artwork(identity), x_restrict=1) for identity in range(100, 117)]
    state.pages = [
        {"illusts": [*blocked, state.artwork(1)], "next_url": next_url(RECOMMENDED, recommended_params())},
        {"illusts": [state.artwork(2)], "next_url": None},
    ]
    contexts = [event_context(), event_context()]
    for index, context in enumerate(contexts):
        event = context.context.event
        if entry == "command":
            assert not [item async for item in plugin.cmd_p(event, "")]
        elif entry == "model_auto":
            outcome = await ImageToolService(plugin).image(context, {})
            assert outcome["delivery"] == {"confirmed": 1}
        else:
            background = await plugin._prepare_checkin_background(event, SimpleNamespace(date_key=plugin.image_index.today_key(), user_id="10001"))
            assert background.illust_id == str(index + 1)
            assert Path(background.image_path).is_file()
            cleanup(background.image_path)
            await plugin._release_checkin_background_claim(event, background)
        feature = "checkin" if entry == "checkin" else "normal"
        expected = CURSOR if index == 0 else None
        assert await plugin.image_index.get_page_cursor("group:1", "pixiv:recommended", feature=feature) == expected
        plugin.image_index.close()
        plugin.image_index = ImageIndexStore(state.directory)
        assert await plugin.image_index.get_page_cursor("group:1", "pixiv:recommended", feature=feature) == expected
    assert [query for _, query in state.calls][1] == dict(recommended_params())
    assert state.image_requests == ["/image/1.png", "/image/2.png"]
    assert not state.pages
    plugin.lolicon_client.random.assert_not_called()
    if entry != "checkin":
        assert all(context.context.event.bot.call_action.await_count == 1 for context in contexts)
        assert (await plugin.checkin_store.find_profile("10001")).coins == 60


@pytest.mark.asyncio
async def test_model_public_cursor_and_recommended_send_use_real_sdk(runtime):
    state, plugin = runtime, runtime.plugin
    first = {"illusts": [state.artwork(1)], "next_url": next_url(RECOMMENDED, recommended_params("opaque-v1_abc"))}
    final = {"illusts": [state.artwork(2)], "next_url": None}
    state.pages = [first, final, first, final]
    service = ImageToolService(plugin)
    context = event_context()
    result = await service.query(context, "artworks", {"mode": "recommended", "limit": 1})
    assert [record["id"] for record in result["records"]] == ["1"]
    result = await service.query(context, "artworks", {"cursor": result["pagination"]["next_cursor"]})
    assert [record["id"] for record in result["records"]] == ["2"]
    assert not state.image_requests
    assert (await plugin.checkin_store.find_profile("10001")).coins == 100
    result = await service.image(context, {"mode": "recommended", "count": 2})
    assert result["delivery"] == {"confirmed": 2} and result["settlement"] == "completed"
    assert dict(recommended_params("opaque-v1_abc")) == state.calls[1][1] == state.calls[3][1]
    assert (await plugin.checkin_store.find_profile("10001")).coins == 60


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["null", "-1", "opaque-v1_abc", "123", ""])
async def test_valid_full_recommendation_batch_does_not_fall_back_or_skip_unused_images(runtime, token):
    state, plugin = runtime, runtime.plugin
    state.pages = [{"illusts": [state.artwork(identity) for identity in range(1, 19)], "next_url": next_url(RECOMMENDED, recommended_params(token))}]
    context = event_context()
    assert not [item async for item in plugin.cmd_p(context.context.event, "")]
    assert context.context.event.bot.call_action.await_count == 1
    assert len(state.image_requests) == 1 and len(state.calls) == 1
    assert (await plugin.checkin_store.find_profile("10001")).coins == 80
    assert await plugin.image_index.get_page_cursor("group:1", "pixiv:recommended") is None
    plugin.lolicon_client.random.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,params", [
    ("search", {"word": "test AND", "search_target": "title_and_caption", "sort": "date_asc", "start_date": "2026-01-01", "end_date": "2026-11-01"}),
    ("ranking", {"mode": "week", "date": "2026-11-01"}),
    ("author", {"user_id": "42", "type": "illust"}),
    ("users", {"word": "artist", "sort": "date_desc"}),
])
async def test_other_query_modes_preserve_conditions_on_sdk_wire(runtime, mode, params):
    state = runtime
    route = {"search": "/v1/search/illust", "ranking": "/v1/illust/ranking", "author": "/v1/user/illusts", "users": "/v1/search/user"}[mode]
    field = "user_previews" if mode == "users" else "illusts"
    state.pages = [{field: [], "next_url": next_url(route, {**params, "offset": 30})}, {field: [], "next_url": None}]
    query = {
        "search": ArtworkQuery(mode="search", tag="test AND", search_target="title_and_caption", sort="date_asc", start_date="2026-01-01", end_date="2026-11-01"),
        "ranking": ArtworkQuery(mode="ranking", ranking_mode="week", date="2026-11-01"),
        "author": ArtworkQuery(mode="author", user_id=42),
    }.get(mode)
    first = await state.client.users_page("artist") if mode == "users" else await state.client.query_page(query)
    await state.client.users_page("artist", first.next_params) if mode == "users" else await state.client.query_page(query, first.next_params)
    assert state.calls == [(route, {**params, "filter": "for_ios"}), (route, {**params, "filter": "for_ios", "offset": "30"})]


@pytest.mark.asyncio
@pytest.mark.parametrize("seeds", [[("seed_illust_ids[0]", "42"), ("seed_illust_ids[1]", "43")], [("seed_illust_ids[]", "42"), ("seed_illust_ids[]", "43")], [("seed_illust_ids[]", "42,43")]])
async def test_related_list_encodings_reach_fixed_authenticated_endpoint(runtime, seeds):
    state = runtime
    route = "/v2/illust/related"
    state.pages = [{"illusts": [], "next_url": next_url(route, [*seeds, ("viewed[0]", "1"), ("viewed[1]", "2")])}, {"illusts": [], "next_url": None}]
    query = ArtworkQuery(mode="related", illust_id=42)
    first = await state.client.query_page(query)
    await state.client.query_page(query, first.next_params)
    assert state.calls[1] == (route, {"illust_id": "42", "filter": "for_ios", "seed_illust_ids[0]": "42", "seed_illust_ids[1]": "43", "viewed[0]": "1", "viewed[1]": "2"})


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    [("offset", "30"), ("offset", "60")], [("offset", "²")],
    [("offset", "-1")], [("viewed[1]", "1")],
    [("viewed[]", "1"), ("viewed[0]", "2")], [("include_ranking_label", "maybe")],
])
async def test_invalid_upstream_cursor_has_classified_error_without_second_request(runtime, params):
    state = runtime
    state.pages = [{"illusts": [state.artwork(1)], "next_url": next_url(RECOMMENDED, params)}]
    with pytest.raises(QueryError) as error:
        await state.client.query_page(ArtworkQuery(mode="recommended"))
    assert error.value.code == "upstream_error"
    assert len(state.calls) == 1
    assert state.client._active_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    {"offset": True}, {"offset": 1.5}, {"offset": 10**21},
    {"offset": 30, "max_bookmark_id_for_recommend": "x" * 257},
    {"offset": 30, "viewed": [True]}, {"offset": 30, "viewed": [1] * 1025},
    {"offset": 30, "url": "https://example.test"},
])
async def test_invalid_persisted_parameters_cannot_overwrite_a_valid_recommendation(runtime, invalid):
    index = runtime.plugin.image_index
    await index.save_page_cursor("group:1", "pixiv:recommended", CURSOR)
    with pytest.raises(ValueError):
        await index.save_page_cursor("group:1", "pixiv:recommended", invalid)
    assert await index.get_page_cursor("group:1", "pixiv:recommended") == CURSOR
