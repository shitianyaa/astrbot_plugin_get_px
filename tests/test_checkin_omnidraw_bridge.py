import threading
from types import SimpleNamespace

import pytest

from checkin.omnidraw_bridge import OmnidrawBridge


class FakeOmnidraw:
    def __init__(
        self,
        *,
        enable_daily_limit=True,
        daily_image_limit=20,
        enable_checkin=False,
        blocked_users="",
        unlimited_users="",
        allowed_users="",
        unlimited_groups="",
        usable_users="",
    ):
        self.plugin_config = SimpleNamespace(
            enable_daily_limit=enable_daily_limit,
            daily_image_limit=daily_image_limit,
            enable_checkin=enable_checkin,
            blocked_users=blocked_users,
            unlimited_users=unlimited_users,
            allowed_users=allowed_users,
            unlimited_groups=unlimited_groups,
            usable_users=usable_users,
        )
        self._usage_lock = threading.RLock()
        self._usage_stats = {"date": "2026-09-06", "total": 0, "users": {}}
        self._quota_reservations = {}
        self.persist_calls = 0

    def _daily_image_limit(self):
        if not self.plugin_config.enable_daily_limit:
            return 0
        return max(1, int(self.plugin_config.daily_image_limit))

    def _normalize_usage_stats(self, stats):
        if isinstance(stats, dict) and stats.get("date") == "2026-09-06":
            return stats
        return {"date": "2026-09-06", "total": 0, "users": {}}

    def _persist_usage_stats(self):
        self.persist_calls += 1


class _DeployedOmnidraw(FakeOmnidraw):
    """模拟部署版 v3.3.23：_usage_lock/_daily_image_limit/_normalize_usage_stats/
    _usage_stats/_persist_usage_stats 都在，但没有 _quota_reservations。"""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        del self._quota_reservations


class _BrokenOmnidraw:
    """缺少桥所依赖的私有成员，模拟版本过旧。"""

    plugin_config = SimpleNamespace(enable_checkin=False)


def _context_with(star, *, activated=True):
    meta = SimpleNamespace(activated=activated, star_cls=star)
    return SimpleNamespace(
        get_registered_star=lambda name: meta
        if name == "astrbot_plugin_omnidraw"
        else None
    )


def _meta(star, *, name="astrbot_plugin_omnidraw", activated=True):
    return SimpleNamespace(
        name=name,
        activated=activated,
        star_cls=star,
        root_dir_name=name,
        module_path=f"data.plugins.{name}",
    )


def _ctx(*, registered=None, all_stars=()):
    return SimpleNamespace(
        get_registered_star=lambda name: registered
        if name == "astrbot_plugin_omnidraw"
        else None,
        get_all_stars=lambda: list(all_stars),
    )


class _ProbePassGrantFails:
    """探测过（_usage_lock + _daily_image_limit 在）但 _grant_sync 访问
    _normalize_usage_stats 时抛 AttributeError，用于锁定发放失败的日志路径。"""

    def __init__(self):
        self.plugin_config = SimpleNamespace(
            enable_daily_limit=True,
            daily_image_limit=20,
            enable_checkin=False,
            blocked_users="",
            unlimited_users="",
            allowed_users="",
            unlimited_groups="",
            usable_users="",
        )
        self._usage_lock = threading.RLock()
        self._usage_stats = {"date": "2026-09-06", "total": 0, "users": {}}
        self._quota_reservations = {}

    def _daily_image_limit(self):
        return 20


def test_snapshot_without_plugin_reports_not_installed():
    bridge = OmnidrawBridge(SimpleNamespace(get_registered_star=lambda name: None))
    status = bridge.snapshot("10001")
    assert not status.installed
    assert not status.compatible
    assert not status.available


def test_snapshot_ignores_inactive_plugin():
    star = FakeOmnidraw()
    bridge = OmnidrawBridge(_context_with(star, activated=False))
    assert not bridge.snapshot("10001").installed


def test_snapshot_reads_switches_and_user_quota():
    star = FakeOmnidraw(enable_checkin=True)
    star._usage_stats["users"]["10001"] = {"count": 6, "bonus": 3, "checkin_at": 1}
    bridge = OmnidrawBridge(_context_with(star))
    status = bridge.snapshot("10001")
    assert status.installed and status.available
    assert status.checkin_enabled
    assert not status.permission_configured
    assert status.bonus == 3
    assert status.remaining == 20 + 3 - 6


def test_snapshot_flags_blocked_and_unlimited_users():
    star = FakeOmnidraw(
        blocked_users="10002",
        unlimited_users="10003",
        allowed_users="10004",
        unlimited_groups="999",
    )
    bridge = OmnidrawBridge(_context_with(star))
    assert bridge.snapshot("10002").blocked
    assert bridge.snapshot("10003").unlimited
    assert bridge.snapshot("10004").unlimited
    assert bridge.snapshot("10005", "999").unlimited
    plain = bridge.snapshot("10006")
    assert not plain.blocked and not plain.unlimited
    assert bridge.snapshot().permission_configured


def test_snapshot_flags_usable_users_whitelist():
    star = FakeOmnidraw(usable_users="10003,10004")
    bridge = OmnidrawBridge(_context_with(star))
    assert bridge.snapshot("10003").usable
    assert not bridge.snapshot("10005").usable
    # 白名单为空时所有人可用
    empty = FakeOmnidraw()
    assert OmnidrawBridge(_context_with(empty)).snapshot("10005").usable


def test_snapshot_incompatible_version_degrades():
    bridge = OmnidrawBridge(_context_with(_BrokenOmnidraw()))
    status = bridge.snapshot("10001")
    assert status.installed
    assert not status.compatible
    assert not status.available


@pytest.mark.asyncio
async def test_grant_adds_bonus_and_persists():
    star = FakeOmnidraw()
    bridge = OmnidrawBridge(_context_with(star))
    first = await bridge.grant("10001", 5, display_name="测试用户")
    second = await bridge.grant("10001", 5)
    assert first.granted and second.granted
    assert first.message == "生图额度 +5 张"
    assert second.bonus == 10
    record = star._usage_stats["users"]["10001"]
    assert record["bonus"] == 10
    assert record["display_name"] == "测试用户"
    assert record["checkin_at"] == 0
    assert star.persist_calls == 2


@pytest.mark.asyncio
async def test_grant_rejects_when_daily_limit_disabled():
    star = FakeOmnidraw(enable_daily_limit=False)
    bridge = OmnidrawBridge(_context_with(star))
    status = await bridge.grant("10001", 5)
    assert not status.granted
    assert "未启用每日生图限制" in status.message
    assert star.persist_calls == 0


@pytest.mark.asyncio
async def test_grant_rejects_nonpositive_amount():
    bridge = OmnidrawBridge(_context_with(FakeOmnidraw()))
    status = await bridge.grant("10001", 0)
    assert not status.granted


@pytest.mark.asyncio
async def test_grant_without_plugin_fails():
    bridge = OmnidrawBridge(SimpleNamespace(get_registered_star=lambda name: None))
    status = await bridge.grant("10001", 5)
    assert not status.granted


@pytest.mark.asyncio
async def test_grant_swallows_unexpected_error():
    star = FakeOmnidraw()

    def _boom():
        raise RuntimeError("disk error")

    star._persist_usage_stats = _boom
    bridge = OmnidrawBridge(_context_with(star))
    status = await bridge.grant("10001", 5)
    assert not status.granted
    assert status.message == "发放失败，请稍后再试"


def test_resolve_prefers_probe_passing_instance():
    bad = _BrokenOmnidraw()  # 缺 _usage_lock → 探测不过
    good = FakeOmnidraw()
    bridge = OmnidrawBridge(
        _ctx(registered=_meta(bad), all_stars=(_meta(good),))
    )
    status = bridge.snapshot("10001")
    assert status.installed and status.available
    assert status.remaining == 20  # 用的是 good 实例


def test_resolve_skips_wrong_name_metadata():
    bad = _BrokenOmnidraw()
    good = FakeOmnidraw()
    bridge = OmnidrawBridge(
        _ctx(
            registered=_meta(bad),
            all_stars=(_meta(good, name="astrbot_plugin_other"),),
        )
    )
    status = bridge.snapshot("10001")
    # 身份不匹配的好实例被跳过 → 回退坏实例 → 不兼容（而非误用 good）
    assert status.installed and not status.compatible


def test_all_candidates_bad_reports_incompatible():
    bad = _BrokenOmnidraw()
    bridge = OmnidrawBridge(
        _ctx(registered=_meta(bad), all_stars=(_meta(bad),))
    )
    status = bridge.snapshot("10001")
    assert status.installed and not status.compatible  # 已注册但实例失效 → 不兼容


@pytest.mark.asyncio
async def test_grant_logs_missing_member_on_attribute_error(monkeypatch):
    captured = []
    spy = SimpleNamespace(
        warning=lambda msg, *a, **k: captured.append(msg),
        info=lambda msg, *a, **k: captured.append(msg),
        debug=lambda msg, *a, **k: captured.append(msg),
    )
    monkeypatch.setattr("checkin.omnidraw_bridge.logger", spy)
    star = _ProbePassGrantFails()
    bridge = OmnidrawBridge(
        _ctx(registered=_meta(star), all_stars=(_meta(star),))
    )
    status = await bridge.grant("10001", 5)
    assert not status.granted
    assert any("_normalize_usage_stats" in m and "reason=" in m for m in captured)
    assert any("私有成员" in m for m in captured)


def test_snapshot_without_quota_reservations_attribute():
    """部署版 v3.3.23 没有 _quota_reservations，getattr 兜底为空字典。"""
    star = _DeployedOmnidraw(enable_checkin=True)
    star._usage_stats["users"]["10001"] = {"count": 6, "bonus": 3, "checkin_at": 1}
    bridge = OmnidrawBridge(_context_with(star))
    status = bridge.snapshot("10001")
    assert status.installed and status.available
    assert status.remaining == 20 + 3 - 6  # reserved=0


@pytest.mark.asyncio
async def test_grant_without_quota_reservations_attribute():
    star = _DeployedOmnidraw()
    bridge = OmnidrawBridge(_context_with(star))
    status = await bridge.grant("10001", 5)
    assert status.granted
    assert status.message == "生图额度 +5 张"
    assert star.persist_calls == 1


@pytest.mark.asyncio
async def test_grant_daily_checkin_bonus_adds_bonus_and_sets_checkin_at():
    star = FakeOmnidraw(enable_checkin=True)
    bridge = OmnidrawBridge(_context_with(star))
    status = await bridge.grant_daily_checkin_bonus("10001", display_name="测试")
    assert status.granted
    assert "+" in status.message and "张" in status.message
    record = star._usage_stats["users"]["10001"]
    assert record["checkin_at"] > 0
    assert record["bonus"] >= 1
    assert record["display_name"] == "测试"
    assert star.persist_calls == 1


@pytest.mark.asyncio
async def test_grant_daily_checkin_bonus_is_idempotent():
    star = FakeOmnidraw(enable_checkin=True)
    bridge = OmnidrawBridge(_context_with(star))
    first = await bridge.grant_daily_checkin_bonus("10001")
    second = await bridge.grant_daily_checkin_bonus("10001")
    assert first.granted
    assert not second.granted
    assert "未重复发放" in second.message
    assert star.persist_calls == 1  # 第二次不落盘


@pytest.mark.asyncio
async def test_grant_daily_checkin_bonus_rejects_when_limit_disabled():
    star = FakeOmnidraw(enable_daily_limit=False, enable_checkin=True)
    bridge = OmnidrawBridge(_context_with(star))
    status = await bridge.grant_daily_checkin_bonus("10001")
    assert not status.granted
    assert star.persist_calls == 0


@pytest.mark.asyncio
async def test_grant_daily_checkin_bonus_without_plugin_fails():
    bridge = OmnidrawBridge(SimpleNamespace(get_registered_star=lambda name: None))
    status = await bridge.grant_daily_checkin_bonus("10001")
    assert not status.granted
