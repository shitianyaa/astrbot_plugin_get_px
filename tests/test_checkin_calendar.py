"""Calendar view-model, event normalization, render and command tests."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import AsyncMock

from PIL import Image as PILImage

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from astrbot_plugin_get_px.checkin import artwork as artwork_mod  # noqa: E402
from astrbot_plugin_get_px.checkin.card import CardBackground  # noqa: E402
from astrbot_plugin_get_px.checkin.calendar import (  # noqa: E402
    CALENDAR_EVENT_LIMIT,
    build_checkin_calendar_data,
    collect_calendar_events,
    get_checkin_calendar_template,
)
from astrbot_plugin_get_px.checkin.models import CheckinRecord  # noqa: E402
from astrbot_plugin_get_px.checkin.quality import CHECKIN_JPEG_QUALITY  # noqa: E402
from astrbot_plugin_get_px.main import GetPxPlugin  # noqa: E402
from astrbot_plugin_get_px.pixiv.safety import ContentSafetyPolicy  # noqa: E402


_BASE_RECORD = CheckinRecord(
    date_key="2026-08-02",
    user_id="10001",
    username="tester",
    bot_name="neko",
    base_coins=60,
    bonus_coins=0,
    coins_reward=60,
    base_affection=0.6,
    bonus_affection=0.0,
    affection_reward=0.6,
    boost_active=False,
    boost_multiplier=1.0,
    total_coins_after=60,
    total_affection_after=0.6,
    total_days_after=1,
    streak_days_after=1,
    note="",
    background_mode="",
    background_source="",
    background_illust_id="",
    background_title="",
    background_author="",
    created_at="",
    updated_at="",
)

def test_cache_identity_contains_independent_safety_lists():
    base = ContentSafetyPolicy(group_id="g1", builtin_terms_enabled=False, custom_terms=["Alpha"], blacklisted_illust_ids=["1"]).cache_identity()
    terms = ContentSafetyPolicy(group_id="g1", builtin_terms_enabled=False, custom_terms=["Beta"], blacklisted_illust_ids=["1"]).cache_identity()
    ids = ContentSafetyPolicy(group_id="g1", builtin_terms_enabled=False, custom_terms=["Alpha"], blacklisted_illust_ids=["2"]).cache_identity()
    assert base["custom_terms"] == ["Alpha"] and base["blacklisted_illust_ids"] == ["1"]
    assert base != terms and base != ids
    equivalent = ContentSafetyPolicy(group_id="g1", builtin_terms_enabled=False, custom_terms=[" Alpha ", "alpha"], blacklisted_illust_ids=["001", "1"]).cache_identity()
    assert equivalent == base


def _record(date_key: str, coins_reward: int) -> CheckinRecord:
    return replace(_BASE_RECORD, date_key=date_key, coins_reward=coins_reward)


def _event(start: str, name: str, *, end: str | None = None, off: bool = True) -> dict:
    return {
        "start": start,
        "end": end or start,
        "name": name,
        "is_off_day": off,
    }


def test_calendar_data_builds_six_week_grid_and_daily_statuses() -> None:
    data = build_checkin_calendar_data(
        month="2026-08",
        today_key="2026-08-15",
        records=[_record("2026-08-02", 60), _record("2026-08-15", 80)],
        background_url="data:image/jpeg;base64,abc",
        background_credit="背景：夜樱 / 画师名 (ID: 12345)",
    )

    assert data["month_label"] == "2026 年 08 月"
    assert data["checked_days"] == 2
    assert data["coins_earned"] == 140
    assert data["as_of_date"] == "2026-08-15"
    assert data["today_key"] == "2026-08-15"
    assert data["empty"] is False
    assert data["background_url"] == "data:image/jpeg;base64,abc"
    assert data["background_credit"] == "背景：夜樱 / 画师名 (ID: 12345)"
    assert data["events"] == []
    assert data["events_hidden_count"] == 0
    assert len(data["weeks"]) == 6
    assert all(len(week) == 7 for week in data["weeks"])
    days = {cell["date_key"]: cell for week in data["weeks"] for cell in week if cell["date_key"]}
    assert days["2026-08-02"] == {
        "day": 2,
        "date_key": "2026-08-02",
        "state": "checked",
        "state_label": "已签到",
        "coins_label": "+60",
    }
    assert days["2026-08-03"]["state"] == "missed"
    assert days["2026-08-16"]["state"] == "future"


def test_calendar_data_marks_leap_day_and_template_is_self_contained() -> None:
    data = build_checkin_calendar_data(
        month="2024-02", today_key="2026-08-15", records=[]
    )
    dates = {cell["date_key"] for week in data["weeks"] for cell in week}
    assert "2024-02-29" in dates
    assert data["empty"] is True
    assert data["background_url"] == ""
    assert data["background_credit"] == ""

    html = get_checkin_calendar_template()
    assert "data:font/woff2;base64," in html
    assert "__CHECKIN_CALENDAR_FONT_DATA__" not in html
    assert "/*__CHECKIN_CALENDAR_CSS__*/" not in html
    assert "LXGWWenKai" in html


def test_calendar_data_rejects_invalid_months_and_dates() -> None:
    for month in ("", "2026-8", "2026-13", "2026-02-30", "2026/08"):
        try:
            build_checkin_calendar_data(
                month=month, today_key="2026-08-15", records=[]
            )
        except ValueError as exc:
            assert "month" in str(exc)
        else:
            raise AssertionError(f"month {month!r} should raise ValueError")
    try:
        build_checkin_calendar_data(
            month="2026-08", today_key="2026/08/15", records=[]
        )
    except ValueError as exc:
        assert "today_key" in str(exc)
    else:
        raise AssertionError("invalid today_key should raise ValueError")


def test_calendar_events_are_normalized_for_current_month() -> None:
    events = [
        _event("2026-10-01", "国庆节"),
        _event("2026-10-02", "国庆节"),  # 同名折叠进 1 日
        _event("2026-10-10", "国庆节", off=False),  # 调休剔除
        _event("2026-10-08", "寒露"),
        _event("2026-10-11", "重阳节"),
        _event("2026-10-16", "世界粮食日"),
        _event("2026-10-23", "霜降"),
        _event("2026-10-24", "程序员节"),
        _event("2026-10-24", "联合国日"),
        _event("2026-10-31", "万圣节前夜"),
        _event("2026-09-15", "中秋外月事件"),  # 月外原始事件也被规则截出
    ]
    data = build_checkin_calendar_data(
        month="2026-10", today_key="2026-10-01", records=[], events=events
    )

    assert data["events"] == [
        {"day": 1, "name": "国庆节"},
        {"day": 8, "name": "寒露"},
        {"day": 11, "name": "重阳节"},
        {"day": 16, "name": "世界粮食日"},
        {"day": 23, "name": "霜降"},
        {"day": 24, "name": "程序员节"},
    ]
    assert data["events_hidden_count"] == 2
    assert len(data["events"]) == CALENDAR_EVENT_LIMIT


def test_calendar_events_keep_passed_events_for_historical_month() -> None:
    data = build_checkin_calendar_data(
        month="2026-08",
        today_key="2026-09-01",
        records=[],
        events=[_event("2026-08-07", "立秋"), _event("2026-08-19", "七夕")],
    )

    assert data["events"] == [{"day": 7, "name": "立秋"}, {"day": 19, "name": "七夕"}]
    assert data["events_hidden_count"] == 0


def test_collect_calendar_events_pulls_solar_terms_and_custom_events() -> None:
    def fake_holiday_lookup(date_key: str):
        if date_key == "2026-08-19":
            return type("Holiday", (), {"name": "七夕", "is_off_day": True})()
        return None

    events = collect_calendar_events(
        month="2026-08",
        holiday_lookup=fake_holiday_lookup,
        custom_events=(
            type("Event", (), {"date_value": "08-29", "name": "纪念日"})(),
            type("Event", (), {"date_value": "07-31", "name": "上月单次"})(),
        ),
    )

    by_day = {(e.start, e.name): e for e in events}
    # lunar_python 实测 2026-08 节气：7 日立秋、23 日处暑
    assert ("2026-08-07", "立秋") in by_day
    assert ("2026-08-23", "处暑") in by_day
    assert ("2026-08-19", "七夕") in by_day
    assert ("2026-08-29", "纪念日") in by_day
    assert not any(e.start == "2026-07-31" for e in events)


def test_calendar_event_names_are_html_escaped() -> None:
    data = build_checkin_calendar_data(
        month="2026-10",
        today_key="2026-10-01",
        records=[],
        events=[_event("2026-10-31", '<b>万圣节</b> & "夜"')],
    )

    # t2i 端点 Jinja2 不开 autoescape，管理员自定义事件名必须后端转义
    assert data["events"] == [
        {"day": 31, "name": "&lt;b&gt;万圣节&lt;/b&gt; &amp; &quot;夜&quot;"}
    ]
    assert data["events_hidden_count"] == 0

def _make_jpeg(path: Path, size: tuple[int, int]) -> None:
    PILImage.new("RGB", size, "white").save(path, format="JPEG")


class _FakeEvent:
    def __init__(self, *, fail_send: bool = False, group_id: str = ""):
        self.fail_send = fail_send
        self.group_id = group_id
        self.sent = []
        self.stopped = False

    def get_sender_id(self):
        return "10001"

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return self.group_id

    def plain_result(self, text: str):
        return text

    def chain_result(self, chain: list):
        return chain

    async def send(self, payload):
        if self.fail_send:
            raise RuntimeError("send failed")
        self.sent.append(payload)

    def stop_event(self) -> None:
        self.stopped = True


class _CalendarStore:
    def __init__(self, records, global_events=()):
        self.records = list(records)
        self.global_events = tuple(global_events)
        self.calls: list[tuple[str, str]] = []

    async def list_month_records(self, *, user_id: str, month: str):
        self.calls.append((user_id, month))
        if month != "2026-08":
            raise ValueError("month must use YYYY-MM")
        return list(self.records)

    async def list_global_events(self):
        return self.global_events


class _CalendarCache:
    def __init__(self, hit: Path | None = None, published: Path | None = None):
        self.hit = hit
        self.published = published
        self.keys: list[str] = []
        self.stored_key: str | None = None

    @staticmethod
    def cache_key(*, date_key, user_id, template_version, view_model) -> str:
        return "stub:key"

    def get(self, date_key, key, *, expected_size):
        self.keys.append(key)
        return self.hit

    async def store(self, date_key, key, renderer, *, expected_size):
        self.stored_key = key
        result = renderer()
        if asyncio.iscoroutine(result):
            await result
        return self.published


class _KeyCapturingCache(_CalendarCache):
    def __init__(self, published: Path | None = None):
        super().__init__(hit=None, published=published)
        self.view_models: list[dict] = []
        self.sizes: list[tuple[int, int]] = []

    def cache_key(self, *, date_key, user_id, template_version, view_model) -> str:
        self.view_models.append(dict(view_model))
        return "stub:key"

    def get(self, date_key, key, *, expected_size):
        self.sizes.append(tuple(expected_size))
        return self.hit

    async def store(self, date_key, key, renderer, *, expected_size):
        self.sizes.append(tuple(expected_size))
        return await super().store(date_key, key, renderer, expected_size=expected_size)


class _PolicyAwareCalendarCache(_KeyCapturingCache):
    def __init__(self, published: Path, trace: list[str]):
        super().__init__(published=published)
        self.trace = trace
        self.entries: dict[str, Path] = {}

    def cache_key(self, *, date_key, user_id, template_version, view_model) -> str:
        self.view_models.append(dict(view_model))
        return json.dumps(
            {
                "date_key": date_key,
                "user_id": user_id,
                "template_version": template_version,
                "view_model": view_model,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def get(self, date_key, key, *, expected_size):
        self.trace.append("cache_get")
        self.sizes.append(tuple(expected_size))
        return self.entries.get(key)

    async def store(self, date_key, key, renderer, *, expected_size):
        self.sizes.append(tuple(expected_size))
        result = renderer()
        if asyncio.iscoroutine(result):
            await result
        self.entries[key] = self.published
        return self.published


class _StoreWithoutEventSource(_CalendarStore):
    list_global_events = None  # 模拟旧版 store 无该接口


async def _collect(async_iterable):
    return [item async for item in async_iterable]


def test_render_calendar_calls_html_render_with_fixed_jpeg_canvas() -> None:
    plugin = object.__new__(GetPxPlugin)
    plugin.html_render = AsyncMock(return_value="calendar.jpg")

    result = asyncio.run(
        plugin._render_checkin_calendar(
            month="2026-08", today_key="2026-08-15", records=[]
        )
    )

    assert result == "calendar.jpg"
    args = plugin.html_render.await_args
    assert args.kwargs["return_url"] is False
    assert args.kwargs["options"] == {
        "full_page": True,
        "type": "jpeg",
        "quality": CHECKIN_JPEG_QUALITY,
        "clip": {"x": 0, "y": 0, "width": 1600, "height": 900},
        "viewport": {"width": 1600, "height": 900},
        "animations": "disabled",
    }


def test_render_calendar_scales_output_by_render_tier() -> None:
    plugin = object.__new__(GetPxPlugin)
    plugin.html_render = AsyncMock(return_value="calendar.jpg")

    result = asyncio.run(
        plugin._render_checkin_calendar(
            month="2026-08", today_key="2026-08-15", records=[], render_tier="极致"
        )
    )

    assert result == "calendar.jpg"
    options = plugin.html_render.await_args.kwargs["options"]
    # 设计画布恒定 1600×900，输出缩放交给端点 device_scale_factor
    assert options["device_scale_factor_level"] == "ultra"
    assert options["clip"] == {"x": 0, "y": 0, "width": 1600, "height": 900}
    assert options["viewport"] == {"width": 1600, "height": 900}


def test_render_calendar_inlines_background_as_data_url(tmp_path, monkeypatch) -> None:
    image = tmp_path / "bg.jpg"
    _make_jpeg(image, size=(300, 400))
    captured: dict[str, object] = {}

    def _fake_build(**kwargs):
        captured.update(kwargs)
        return {"weeks": []}

    monkeypatch.setattr(artwork_mod, "build_checkin_calendar_data", _fake_build)
    plugin = object.__new__(GetPxPlugin)
    plugin.html_render = AsyncMock(return_value="calendar.jpg")

    asyncio.run(
        plugin._render_checkin_calendar(
            month="2026-08",
            today_key="2026-08-15",
            records=[],
            background=CardBackground(
                image_path=str(image),
                mode="pixiv_daily",
                illust_id="12345",
                title="夜樱",
                author="画师名",
            ),
        )
    )

    assert str(captured["background_url"]).startswith("data:image/jpeg;base64,")
    assert captured["background_credit"] == "背景：夜樱 / 画师名 (ID: 12345)"


def test_calendar_command_reuses_cached_image_without_rerendering(tmp_path) -> None:
    cached = tmp_path / "calendar.jpg"
    _make_jpeg(cached, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = _CalendarCache(hit=cached)
    plugin._render_checkin_calendar = AsyncMock(
        side_effect=AssertionError("should not rerender")
    )
    plugin._prepare_checkin_calendar_background = AsyncMock(
        side_effect=AssertionError("should not fetch background on cache hit")
    )
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    assert output == []
    assert len(event.sent) == 1
    assert cached.exists()  # 缓存归缓存管理，命令层不删除


def test_calendar_command_renders_when_miss_then_publishes_and_sends(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = _CalendarCache(hit=None, published=rendered)
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    assert output == []
    assert plugin.checkin_cache.stored_key is not None
    assert plugin._prepare_checkin_calendar_background.await_count == 1
    assert len(event.sent) == 1


def test_calendar_command_sends_even_when_background_unavailable(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = _CalendarCache(hit=None, published=rendered)
    plugin._prepare_checkin_calendar_background = AsyncMock(
        side_effect=RuntimeError("network down")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    # 取图失败只降级为无背景，不阻塞日历发送
    assert output == []
    assert len(event.sent) == 1
    assert plugin._render_checkin_calendar.await_args.kwargs["background"] is None


def test_calendar_command_cleans_pixiv_background_temp_file_after_send(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    bg_original = tmp_path / "bg-original.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    _make_jpeg(bg_original, size=(300, 400))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = _CalendarCache(hit=None, published=rendered)
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(
            image_path=str(bg_original),
            mode="pixiv_daily",
            illust_id="12345",
            title="夜樱",
            author="画师名",
        )
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    assert output == []
    assert len(event.sent) == 1
    assert not bg_original.exists()  # 背景原图渲染后即清理
    assert rendered.exists()  # 已发布缓存图保留


def test_calendar_command_falls_back_to_direct_render_without_cache(tmp_path) -> None:
    rendered = tmp_path / "calendar.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = None
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    assert output == []
    assert len(event.sent) == 1
    assert not rendered.exists()  # 无缓存回退路径会清理临时文件


def test_calendar_command_returns_text_for_bad_month_render_and_send_failures(
    tmp_path,
) -> None:
    # 1) 非法月份 → 纯文本提示
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    event = _FakeEvent()
    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-13")))
    assert len(output) == 1
    assert "YYYY-MM" in output[0]
    assert event.sent == []

    # 2) 渲染抛错 → 纯文本提示
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = None
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(side_effect=RuntimeError("render"))
    event = _FakeEvent()
    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))
    assert len(output) == 1
    assert "生成失败" in output[0]

    # 3) 直渲路径发送失败 → 临时渲染文件已清理
    rendered = tmp_path / "calendar.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    plugin.checkin_cache = None
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent(fail_send=True)
    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))
    assert len(output) == 1
    assert "生成失败" in output[0]
    assert not rendered.exists()


def test_calendar_cache_key_reflects_custom_event_fingerprint(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(
        records=[],
        global_events=(type("Event", (), {"event_id": 7})(),),
    )
    cache = _KeyCapturingCache(published=rendered)
    plugin.checkin_cache = cache
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    # 管理员增删自定义事件后指纹变化，当日缓存即失效重渲
    assert output == []
    assert cache.view_models == [
        {
            "month": "2026-08",
            "events": "1-7",
            "records": "",
            "background_quality": "medium",
            "output_size": "1600x900",
            "content_safety_policy": ContentSafetyPolicy().cache_identity(),
        }
    ]
    assert len(event.sent) == 1


def test_calendar_cache_key_fingerprint_blank_without_event_source(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _StoreWithoutEventSource(records=[])
    cache = _KeyCapturingCache(published=rendered)
    plugin.checkin_cache = cache
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    assert output == []
    assert cache.view_models == [
        {
            "month": "2026-08",
            "events": "",
            "records": "",
            "background_quality": "medium",
            "output_size": "1600x900",
            "content_safety_policy": ContentSafetyPolicy().cache_identity(),
        }
    ]


def test_calendar_background_quality_follows_render_tier(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True, "checkin_card_quality_tier": "清晰"}
    plugin.checkin_store = _CalendarStore(records=[])
    cache = _KeyCapturingCache(published=rendered)
    plugin.checkin_cache = cache
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    # 档位映射与签到卡一致：清晰/极致 → large 背景；质量参与缓存 key 换档即重渲
    assert output == []
    assert cache.view_models == [
        {
            "month": "2026-08",
            "events": "0-0",
            "records": "",
            "background_quality": "large",
            "output_size": "2080x1170",
            "content_safety_policy": ContentSafetyPolicy().cache_identity(),
        }
    ]
    assert plugin._prepare_checkin_calendar_background.await_args.kwargs[
        "background_quality"
    ] == "large"


def test_calendar_cache_expected_size_follows_tier(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(2880, 1620))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True, "checkin_card_quality_tier": "极致"}
    plugin.checkin_store = _CalendarStore(records=[])
    cache = _KeyCapturingCache(published=rendered)
    plugin.checkin_cache = cache
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    # 缓存读写按档位输出尺寸校验；清晰与极致背景同为 large，靠 output_size 区分缓存键
    assert output == []
    assert cache.sizes == [(2880, 1620), (2880, 1620)]
    assert cache.view_models[-1]["output_size"] == "2880x1620"


def test_prepare_calendar_background_downloads_requested_quality(
    tmp_path, monkeypatch
) -> None:
    image = tmp_path / "bg.jpg"
    _make_jpeg(image, size=(1600, 900))
    illust = {"id": "123", "title": "夜樱", "user": {"name": "画师"}}
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {}
    plugin.image_index = None
    plugin.downloader = type("Downloader", (), {})()
    plugin.downloader.download_for_send = AsyncMock(
        return_value=(str(image), "large", 1234)
    )
    plugin._fetch_source_candidates = AsyncMock(
        return_value=([illust], 1, "lolicon:test")
    )
    plugin._filter_blacklisted_illusts = AsyncMock(
        side_effect=lambda illusts, policy=None: list(illusts)
    )
    plugin._blacklist_reason_for_illust = AsyncMock(return_value="")
    monkeypatch.setattr(
        artwork_mod,
        "filter_illusts_by_aspect_ratio",
        lambda illusts, *args, **kwargs: list(illusts),
    )
    event = _FakeEvent()

    background = asyncio.run(
        plugin._prepare_checkin_calendar_background(
            event, user_id="10001", background_quality="large"
        )
    )

    assert background is not None and background.mode == "pixiv_daily"
    assert plugin.downloader.download_for_send.await_args.args[1] == "large"


def test_calendar_cache_key_reflects_month_record_state(tmp_path) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[_record("2026-08-02", 60)])
    cache = _KeyCapturingCache(published=rendered)
    plugin.checkin_cache = cache
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))
    event = _FakeEvent()

    output = asyncio.run(_collect(plugin._handle_checkin_calendar(event, "2026-08")))

    # 当月签到记录摘要进入缓存键：当日签到后重看日历不再命中旧图
    assert output == []
    assert cache.view_models == [
        {
            "month": "2026-08",
            "events": "0-0",
            "records": "2026-08-02:60",
            "background_quality": "medium",
            "output_size": "1600x900",
            "content_safety_policy": ContentSafetyPolicy().cache_identity(),
        }
    ]


def _assert_calendar_policy_transition_invalidates_cache(
    tmp_path: Path,
    first_event: _FakeEvent,
    first_policy: ContentSafetyPolicy,
    second_event: _FakeEvent,
    second_policy: ContentSafetyPolicy,
) -> None:
    rendered = tmp_path / "rendered.jpg"
    _make_jpeg(rendered, size=(1600, 900))
    trace: list[str] = []
    plugin = object.__new__(GetPxPlugin)
    plugin.config = {"checkin_enabled": True}
    plugin.checkin_store = _CalendarStore(records=[])
    cache = _PolicyAwareCalendarCache(rendered, trace)
    plugin.checkin_cache = cache
    policies = iter((first_policy, second_policy))

    async def resolve_policy(event):
        trace.append(f"policy:{event.get_group_id() or 'private'}")
        return next(policies)

    plugin._content_safety_policy = AsyncMock(side_effect=resolve_policy)
    plugin._prepare_checkin_calendar_background = AsyncMock(
        return_value=CardBackground(mode="fallback", source="fallback")
    )
    plugin._render_checkin_calendar = AsyncMock(return_value=str(rendered))

    first_output = asyncio.run(
        _collect(plugin._handle_checkin_calendar(first_event, "2026-08"))
    )
    second_output = asyncio.run(
        _collect(plugin._handle_checkin_calendar(second_event, "2026-08"))
    )

    assert first_output == second_output == []
    assert trace == [
        f"policy:{first_event.get_group_id() or 'private'}",
        "cache_get",
        f"policy:{second_event.get_group_id() or 'private'}",
        "cache_get",
    ]
    assert plugin._render_checkin_calendar.await_count == 2
    assert plugin._prepare_checkin_calendar_background.await_count == 2
    assert [
        call.kwargs["policy"]
        for call in plugin._prepare_checkin_calendar_background.await_args_list
    ] == [first_policy, second_policy]
    assert [model["content_safety_policy"] for model in cache.view_models] == [
        first_policy.cache_identity(),
        second_policy.cache_identity(),
    ]


def test_calendar_cache_separates_relaxed_group_from_strict_group(tmp_path) -> None:
    _assert_calendar_policy_transition_invalidates_cache(
        tmp_path,
        _FakeEvent(group_id="group-a"),
        ContentSafetyPolicy(False, False, "group-a"),
        _FakeEvent(group_id="group-b"),
        ContentSafetyPolicy(True, True, "group-b"),
    )


def test_calendar_cache_invalidates_when_same_group_reenables_safety(tmp_path) -> None:
    _assert_calendar_policy_transition_invalidates_cache(
        tmp_path,
        _FakeEvent(group_id="group-a"),
        ContentSafetyPolicy(False, False, "group-a"),
        _FakeEvent(group_id="group-a"),
        ContentSafetyPolicy(True, True, "group-a"),
    )


def test_calendar_cache_separates_relaxed_group_from_private_chat(tmp_path) -> None:
    _assert_calendar_policy_transition_invalidates_cache(
        tmp_path,
        _FakeEvent(group_id="group-a"),
        ContentSafetyPolicy(False, False, "group-a"),
        _FakeEvent(),
        ContentSafetyPolicy(),
    )

def test_calendar_cache_misses_when_only_custom_terms_change(tmp_path) -> None:
    first = ContentSafetyPolicy(False, False, "group-a", custom_terms=("alpha",), blacklisted_illust_ids=("1",))
    second = ContentSafetyPolicy(False, False, "group-a", custom_terms=("beta",), blacklisted_illust_ids=("1",))
    _assert_calendar_policy_transition_invalidates_cache(tmp_path, _FakeEvent(group_id="group-a"), first, _FakeEvent(group_id="group-a"), second)
    assert first.cache_identity()["custom_terms"] != second.cache_identity()["custom_terms"]

def test_calendar_cache_misses_when_only_blacklisted_ids_change(tmp_path) -> None:
    first = ContentSafetyPolicy(False, False, "group-a", custom_terms=("alpha",), blacklisted_illust_ids=("1",))
    second = ContentSafetyPolicy(False, False, "group-a", custom_terms=("alpha",), blacklisted_illust_ids=("2",))
    _assert_calendar_policy_transition_invalidates_cache(tmp_path, _FakeEvent(group_id="group-a"), first, _FakeEvent(group_id="group-a"), second)
    assert first.cache_identity()["blacklisted_illust_ids"] != second.cache_identity()["blacklisted_illust_ids"]


class _FailingEventStore(_CalendarStore):
    async def list_global_events(self):
        raise RuntimeError("db down")


def test_collect_calendar_raw_events_degrades_when_global_events_fail() -> None:
    degraded = object.__new__(GetPxPlugin)
    degraded.checkin_store = _FailingEventStore(records=[])
    healthy = object.__new__(GetPxPlugin)
    healthy.checkin_store = _CalendarStore(records=[])

    # 全局事件读库失败按空事件降级，与无自定义事件的结果一致，不抛异常
    assert asyncio.run(degraded._collect_calendar_raw_events("2026-08")) == (
        asyncio.run(healthy._collect_calendar_raw_events("2026-08"))
    )


def test_calendar_data_escapes_background_credit_markup() -> None:
    # 背景署名来自 Pixiv 抓取文案，t2i 端点 Jinja2 不开 autoescape，出模块前必须转义
    data = build_checkin_calendar_data(
        month="2026-08",
        today_key="2026-08-15",
        records=[],
        background_credit='<b>画师</b> & "夜樱"',
    )
    assert data["background_credit"] == (
        "&lt;b&gt;画师&lt;/b&gt; &amp; &quot;夜樱&quot;"
    )

def test_checkin_calendar_module_retains_default_safety_independence():
    from pixiv.safety import STRICT_CONTENT_SAFETY_POLICY
    assert STRICT_CONTENT_SAFETY_POLICY.general_only_enabled is True
