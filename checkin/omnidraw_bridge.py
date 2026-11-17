from __future__ import annotations

import asyncio
import random
import re
import time
from dataclasses import dataclass

from astrbot.api import logger

LOG_PREFIX = "[GetPx]"
OMNIDRAW_PLUGIN_NAME = "astrbot_plugin_omnidraw"

# 与万象画卷 cmd_checkin 的默认记录结构保持一致，缺用户时按此补齐
_EMPTY_RECORD = {
    "user_id": "",
    "count": 0,
    "last_at": 0,
    "bonus": 0,
    "checkin_at": 0,
}


@dataclass(frozen=True)
class OmnidrawStatus:
    installed: bool = False
    compatible: bool = True
    daily_limit_enabled: bool = False
    checkin_enabled: bool = False
    permission_configured: bool = False
    blocked: bool = False
    usable: bool = True
    unlimited: bool = False
    granted: bool = False
    base_limit: int = 0
    used: int = 0
    bonus: int = 0
    remaining: int = 0
    message: str = ""

    @property
    def available(self) -> bool:
        return self.installed and self.compatible and self.daily_limit_enabled


def _config_id_set(value: object) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        source = value
    else:
        source = re.split(r"[\s,]+", str(value or "").replace("\r", "\n"))
    return {str(item).strip() for item in source if str(item).strip()}


class OmnidrawBridge:
    """读取万象画卷额度状态并发放加成。

    ponytail: 直接依赖对方私有成员（_usage_lock/_usage_stats/_persist_usage_stats/
    _daily_image_limit），部署版 v3.3.23 实测可用；_quota_reservations 在
    部署版中不存在，用 getattr 兜底为空字典。对方升级重命名时
    AttributeError 会被降级为"不可用"，届时再 fork 补公开方法。
    """

    def __init__(self, context, plugin_name: str = OMNIDRAW_PLUGIN_NAME):
        self._context = context
        self._plugin_name = plugin_name

    def _probe_star(self, star) -> bool:
        """轻量探测：实例是否挂齐桥所依赖的私有成员（_usage_lock 是 __init__ 设的实例属性，
        能区分热重载残留 / 半初始化的失效实例）。不触锁、无副作用。"""
        return star is not None and hasattr(star, "_usage_lock") and callable(
            getattr(star, "_daily_image_limit", None)
        )

    def _identity_matches(self, metadata) -> bool:
        name = self._plugin_name
        module_path = str(getattr(metadata, "module_path", "") or "")
        return (
            getattr(metadata, "name", None) == name
            or getattr(metadata, "root_dir_name", None) == name
            or module_path.endswith(name)
        )

    def _resolve_star(self):
        try:
            meta = self._context.get_registered_star(self._plugin_name)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 查询万象画卷插件状态失败: "
                f"error_type={type(exc).__name__} reason={exc}"
            )
            return None
        primary = (
            getattr(meta, "star_cls", None)
            if meta is not None and getattr(meta, "activated", False)
            else None
        )
        if self._probe_star(primary):
            return primary
        # get_registered_star 可能命中热重载残留的失效实例，扫实时注册表找通过探测的实例。
        get_all = getattr(self._context, "get_all_stars", None)
        if callable(get_all):
            try:
                stars = list(get_all() or [])
            except Exception as exc:
                logger.warning(
                    f"{LOG_PREFIX} 枚举万象画卷插件列表失败: "
                    f"error_type={type(exc).__name__} reason={exc}"
                )
                stars = []
            seen: set[int] = {id(primary)} if primary is not None else set()
            for metadata in stars[:10]:
                if not getattr(metadata, "activated", False):
                    continue
                if not self._identity_matches(metadata):
                    continue
                star = getattr(metadata, "star_cls", None)
                if id(star) in seen:
                    continue
                seen.add(id(star))
                if self._probe_star(star):
                    return star
        # 扫不到可用的就原样返回主实例（可能为 None 或失效实例），
        # 由 snapshot/grant 的 try/except 决定降级语义（未安装 vs 不兼容）。
        return primary

    def _incompatible(self, exc: BaseException | None = None) -> OmnidrawStatus:
        reason = f" reason={exc}" if exc is not None else ""
        logger.info(
            f"{LOG_PREFIX} 万象画卷版本过旧或不兼容，生图额度功能不可用{reason}"
        )
        return OmnidrawStatus(installed=True, compatible=False)

    def snapshot(self, user_id: str = "", group_id: str = "") -> OmnidrawStatus:
        star = self._resolve_star()
        if star is None:
            return OmnidrawStatus(installed=False, compatible=False)
        config = getattr(star, "plugin_config", None)
        try:
            limit = int(star._daily_image_limit() or 0)
            with star._usage_lock:
                stats = star._normalize_usage_stats(star._usage_stats)
                record = stats.get("users", {}).get(user_id, {})
                bonus = int(record.get("bonus", 0) or 0)
                used = int(record.get("count", 0) or 0)
                reserved = int(getattr(star, "_quota_reservations", {}).get(user_id, 0) or 0)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 万象画卷状态读取失败: "
                f"error_type={type(exc).__name__} reason={exc}"
            )
            return self._incompatible(exc)
        blocked_ids = _config_id_set(getattr(config, "blocked_users", ""))
        unlimited_ids = _config_id_set(
            getattr(config, "unlimited_users", "")
        ) | _config_id_set(getattr(config, "allowed_users", ""))
        unlimited_groups = _config_id_set(getattr(config, "unlimited_groups", ""))
        usable_ids = _config_id_set(getattr(config, "usable_users", ""))
        return OmnidrawStatus(
            installed=True,
            daily_limit_enabled=limit > 0,
            checkin_enabled=bool(getattr(config, "enable_checkin", False)),
            permission_configured=any(
                (blocked_ids, unlimited_ids, unlimited_groups, usable_ids)
            ),
            blocked=bool(user_id) and user_id in blocked_ids,
            usable=not usable_ids or (bool(user_id) and user_id in usable_ids),
            unlimited=bool(user_id)
            and (user_id in unlimited_ids or bool(group_id and group_id in unlimited_groups)),
            base_limit=limit,
            used=used,
            bonus=bonus,
            remaining=max(0, limit + bonus - used - reserved) if limit > 0 else 0,
        )

    async def grant(
        self, user_id: str, amount: int, *, display_name: str = ""
    ) -> OmnidrawStatus:
        if amount <= 0:
            return OmnidrawStatus(message="张数无效")
        star = self._resolve_star()
        if star is None:
            return OmnidrawStatus(message="未检测到万象画卷插件")
        try:
            return await asyncio.to_thread(
                self._grant_sync, star, user_id, amount, display_name
            )
        except (AttributeError, TypeError) as exc:
            logger.warning(
                f"{LOG_PREFIX} 生图额度发放失败(私有成员): "
                f"user_id={user_id} reason={exc}"
            )
            return self._incompatible(exc)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 生图额度发放失败: "
                f"user_id={user_id} error_type={type(exc).__name__} reason={exc}"
            )
            return OmnidrawStatus(message="发放失败，请稍后再试")

    def _grant_sync(
        self, star, user_id: str, amount: int, display_name: str
    ) -> OmnidrawStatus:
        limit = int(star._daily_image_limit() or 0)
        if limit <= 0:
            return OmnidrawStatus(
                installed=True, message="万象画卷未启用每日生图限制"
            )
        with star._usage_lock:
            stats = star._normalize_usage_stats(star._usage_stats)
            star._usage_stats = stats
            users = stats.setdefault("users", {})
            record = users.setdefault(user_id, dict(_EMPTY_RECORD))
            record["user_id"] = user_id
            record["bonus"] = int(record.get("bonus", 0) or 0) + amount
            if display_name:
                record["display_name"] = display_name
            stats["total"] = sum(
                int(item.get("count", 0) or 0) for item in users.values()
            )
            star._persist_usage_stats()
            bonus = int(record["bonus"])
            used = int(record.get("count", 0) or 0)
            reserved = int(getattr(star, "_quota_reservations", {}).get(user_id, 0) or 0)
        logger.info(
            f"{LOG_PREFIX} 生图额度发放完成: "
            f"user_id={user_id} amount={amount} bonus={bonus}"
        )
        return OmnidrawStatus(
            installed=True,
            daily_limit_enabled=True,
            granted=True,
            bonus=bonus,
            remaining=max(0, limit + bonus - used - reserved),
            message=f"生图额度 +{amount} 张",
        )

    async def grant_daily_checkin_bonus(
        self, user_id: str, *, display_name: str = ""
    ) -> OmnidrawStatus:
        """签到联动：复刻万象画卷 /签到 的额度发放（随机区间 + checkin_at 幂等）。

        与 shop.grant 的区别：写 checkin_at（与对方 /签到 双向幂等）、
        随机区间来自对方 checkin_bonus_min/max、不改 checkin_at=0 的语义。
        """
        star = self._resolve_star()
        if star is None:
            return OmnidrawStatus(message="未检测到万象画卷插件")
        try:
            return await asyncio.to_thread(
                self._grant_checkin_sync, star, user_id, display_name
            )
        except (AttributeError, TypeError) as exc:
            logger.warning(
                f"{LOG_PREFIX} 签到联动额度发放失败(私有成员): "
                f"user_id={user_id} reason={exc}"
            )
            return self._incompatible(exc)
        except Exception as exc:
            logger.warning(
                f"{LOG_PREFIX} 签到联动额度发放失败: "
                f"user_id={user_id} error_type={type(exc).__name__} reason={exc}"
            )
            return OmnidrawStatus(message="发放失败，请稍后再试")

    def _grant_checkin_sync(
        self, star, user_id: str, display_name: str
    ) -> OmnidrawStatus:
        limit = int(star._daily_image_limit() or 0)
        if limit <= 0:
            return OmnidrawStatus(
                installed=True, message="万象画卷未启用每日生图限制"
            )
        config = getattr(star, "plugin_config", None)
        bonus_min = int(getattr(config, "checkin_bonus_min", 1) or 1)
        bonus_max = int(getattr(config, "checkin_bonus_max", 3) or 3)
        if bonus_max < bonus_min:
            bonus_max = bonus_min
        gained = (
            random.randint(bonus_min, bonus_max) if bonus_max > bonus_min else bonus_min
        )
        reservations = getattr(star, "_quota_reservations", {})
        with star._usage_lock:
            stats = star._normalize_usage_stats(star._usage_stats)
            star._usage_stats = stats
            users = stats.setdefault("users", {})
            record = users.setdefault(user_id, dict(_EMPTY_RECORD))
            if int(record.get("checkin_at", 0) or 0) > 0:
                bonus = int(record.get("bonus", 0) or 0)
                used = int(record.get("count", 0) or 0)
                reserved = int(reservations.get(user_id, 0) or 0)
                return OmnidrawStatus(
                    installed=True,
                    daily_limit_enabled=True,
                    bonus=bonus,
                    remaining=max(0, limit + bonus - used - reserved),
                    message="今日已签到，生图额度未重复发放",
                )
            record["user_id"] = user_id
            record["bonus"] = int(record.get("bonus", 0) or 0) + gained
            record["checkin_at"] = int(time.time())
            if display_name:
                record["display_name"] = display_name
            stats["total"] = sum(
                int(item.get("count", 0) or 0) for item in users.values()
            )
            star._persist_usage_stats()
            bonus = int(record["bonus"])
            used = int(record.get("count", 0) or 0)
            reserved = int(reservations.get(user_id, 0) or 0)
        logger.info(
            f"{LOG_PREFIX} 签到联动额度发放完成: "
            f"user_id={user_id} gained={gained} bonus={bonus}"
        )
        return OmnidrawStatus(
            installed=True,
            daily_limit_enabled=True,
            granted=True,
            bonus=bonus,
            remaining=max(0, limit + bonus - used - reserved),
            message=f"生图额度 +{gained} 张",
        )

    def log_coexistence_hint(self) -> None:
        status = self.snapshot()
        if not status.installed:
            return
        if not status.compatible:
            logger.info(
                f"{LOG_PREFIX} 检测到万象画卷，但版本过旧，生图额度功能不可用"
            )
            return
        if not status.daily_limit_enabled:
            logger.info(
                f"{LOG_PREFIX} 检测到万象画卷：未启用每日生图限制，"
                "签到商店不展示生图额度商品，启用后自动展示"
            )
            return
        if status.checkin_enabled:
            logger.info(
                f"{LOG_PREFIX} 检测到万象画卷已开启签到领额度，"
                "本插件 /签到 将自动发放金币与生图额度"
                "（万象画卷的 /签到 已被 stop_event 屏蔽，无需手动禁用）"
            )
        else:
            logger.info(
                f"{LOG_PREFIX} 检测到万象画卷：签到商店已展示生图额度商品"
            )
