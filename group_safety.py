from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import uuid

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession

if __package__:
    from .pixiv.safety import (
        ContentSafetyPolicy,
        MATCH_MODES,
        normalize_illust_ids,
        normalize_terms,
        normalize_safety_text,
    )
else:
    from pixiv.safety import (
        ContentSafetyPolicy,
        MATCH_MODES,
        normalize_illust_ids,
        normalize_terms,
        normalize_safety_text,
    )


CONFIG_KEY = "safety_groups"
TERMS_KEY = "global_safety_terms"
IDS_KEY = "global_blacklisted_illust_ids"
MODE_KEY = "keyword_match_mode"
VERSION_KEY = "safety_rules_version"
LEGACY_KEYS = (
    "group_content_safety_policies",
    "private_content_safety_policies",
    "group_content_safety_policies_migrated",
)
SAFETY_VERSION = 1


def normalize_session(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("会话必须为 /sid 返回的完整 UMO")
    value = value.strip()
    try:
        session = MessageSession.from_str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("会话必须为 /sid 返回的完整 UMO") from exc
    if not session.platform_id.strip() or not session.session_id.strip():
        raise ValueError("会话必须包含平台实例和会话 ID")
    return str(session)


def normalize_groups(raw: object) -> list[dict]:
    if not isinstance(raw, list):
        raise ValueError("安全分组必须是列表")
    groups, ids, names, sessions = [], set(), set(), set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("安全分组必须是对象")
        group_id = item.get("group_id", "")
        name = item.get("name", "")
        if not group_id and isinstance(name, str) and name.strip():
            group_id = uuid.uuid5(
                uuid.NAMESPACE_URL, f"get-px-safety:{name.strip()}"
            ).hex
        if not isinstance(group_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,64}", group_id
        ):
            raise ValueError("分组 ID 无效")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 64
            or any(ord(c) < 32 for c in name)
        ):
            raise ValueError("分组名称必须为 1–64 个字符")
        if group_id in ids or name.strip().casefold() in names:
            raise ValueError("分组 ID 或名称重复")
        targets = item.get("sessions", [])
        if not isinstance(targets, list):
            raise ValueError("关联会话必须是列表")
        normalized = [normalize_session(value) for value in targets]
        if len(set(normalized)) != len(normalized) or sessions.intersection(normalized):
            raise ValueError("一个会话只能属于一个分组")
        allow_r18 = item.get("allow_r18", False)
        if type(allow_r18) is not bool:
            raise ValueError("允许 R18 必须是布尔值")
        groups.append(
            {
                "__template_key": "safety_group",
                "group_id": group_id,
                "name": name.strip(),
                "sessions": normalized,
                "allow_r18": allow_r18,
                "custom_terms": list(normalize_terms(item.get("custom_terms", []))),
            }
        )
        ids.add(group_id)
        names.add(name.strip().casefold())
        sessions.update(normalized)
    return groups


class SessionSafetyService:
    """Schema 和 Pages 共用的全局规则与命名分组。"""

    def __init__(self, config, *, log_prefix: str = "[GetPx]"):
        self.config = config
        self.log_prefix = log_prefix
        self._lock = asyncio.Lock()
        self.data_dir: Path | None = None

    def _container(self):
        grouped = self.config.get("content_dedupe")
        if not isinstance(grouped, dict):
            raise ValueError("安全配置分组无效")
        return grouped

    def _snapshot(self) -> dict:
        container = self._container()
        mode = container.get(MODE_KEY, "宽松")
        if mode not in MATCH_MODES:
            raise ValueError("关键词匹配方式无效")
        return {
            MODE_KEY: mode,
            TERMS_KEY: list(normalize_terms(container.get(TERMS_KEY, []))),
            IDS_KEY: list(normalize_illust_ids(container.get(IDS_KEY, []))),
            CONFIG_KEY: normalize_groups(container.get(CONFIG_KEY, [])),
        }

    def _save(self, changes: dict) -> None:
        container = self._container()
        previous = deepcopy(container)
        refs = {
            key: value for key, value in container.items() if isinstance(value, list)
        }
        try:
            for key, value in changes.items():
                if isinstance(container.get(key), list) and isinstance(value, list):
                    container[key][:] = deepcopy(value)
                else:
                    container[key] = deepcopy(value)
            self.config.save_config()
        except Exception:
            restored = deepcopy(previous)
            for key, ref in refs.items():
                ref[:] = restored[key]
                restored[key] = ref
            container.clear()
            container.update(restored)
            raise

    def _write_archive(self, legacy: dict) -> Path:
        if self.data_dir is None:
            raise RuntimeError("安全归档目录尚未初始化")
        encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        root = self.data_dir / "safety_legacy_backups"
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"legacy-{digest}.json"
        expected = {
            "schema_version": 1,
            "kind": "legacy_safety_archive",
            "legacy": legacy,
        }
        if not target.exists():
            temporary = target.with_suffix(".tmp")
            try:
                temporary.write_text(
                    json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                # 先核对临时文件，成功才发布；重试以内容哈希避免重复归档。
                if json.loads(temporary.read_text(encoding="utf-8")) != expected:
                    raise RuntimeError("旧策略备份校验失败")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        if json.loads(target.read_text(encoding="utf-8")) != expected:
            raise RuntimeError("旧策略备份校验失败")
        return target

    async def initialize(self, legacy_store, image_index, data_dir: Path | str) -> None:
        self.data_dir = Path(data_dir)
        async with self._lock:
            container = self._container()
            version = container.get(VERSION_KEY, 0)
            if type(version) is not int or version not in (0, SAFETY_VERSION):
                raise ValueError("不支持的安全配置版本")
            settings = self._snapshot()
            if version == SAFETY_VERSION:
                if container.get(CONFIG_KEY, []) != settings[CONFIG_KEY]:
                    self._save({CONFIG_KEY: settings[CONFIG_KEY]})
                return
            legacy_records = await legacy_store.list_group_content_safety_records()
            legacy_terms = await image_index.list_safety_terms()
            legacy_ids = await image_index.list_blacklist_illusts()
            old = {key: deepcopy(container.get(key)) for key in LEGACY_KEYS}
            legacy = {
                "config": old,
                "database_policies": legacy_records,
                "global_terms": legacy_terms,
                "global_blacklist": legacy_ids,
            }
            if (
                legacy_records
                or legacy_terms
                or legacy_ids
                or any(old.get(key) for key in LEGACY_KEYS[:2])
            ):
                path = await asyncio.to_thread(self._write_archive, legacy)
                logger.info(
                    f"{self.log_prefix} 旧安全策略已备份: file={path.name} restore_supported=no"
                )
            # 全局人工数据保留；旧局部策略不猜测 UMO，也不继承旧 R18 开关。
            terms = normalize_terms(
                [
                    *container.get(TERMS_KEY, []),
                    *(item["term"] for item in legacy_terms),
                ]
            )
            ids = normalize_illust_ids(
                [
                    *container.get(IDS_KEY, []),
                    *(str(item["illust_id"]) for item in legacy_ids),
                ]
            )
            self._save(
                {
                    TERMS_KEY: list(terms),
                    IDS_KEY: list(ids),
                    CONFIG_KEY: settings[CONFIG_KEY],
                    VERSION_KEY: SAFETY_VERSION,
                    LEGACY_KEYS[0]: [],
                    LEGACY_KEYS[1]: [],
                    LEGACY_KEYS[2]: True,
                }
            )

    async def settings(self) -> dict:
        async with self._lock:
            return deepcopy(self._snapshot())

    async def resolve(self, session: str) -> ContentSafetyPolicy:
        session = normalize_session(session)
        async with self._lock:
            settings = self._snapshot()
            selected = next(
                (
                    group
                    for group in settings[CONFIG_KEY]
                    if session in group["sessions"]
                ),
                None,
            )
            terms = [
                *settings[TERMS_KEY],
                *(selected["custom_terms"] if selected else []),
            ]
            return ContentSafetyPolicy(
                general_only_enabled=not (selected and selected["allow_r18"]),
                match_mode=settings[MODE_KEY],
                custom_terms=tuple(terms),
                blacklisted_illust_ids=tuple(settings[IDS_KEY]),
            )

    async def global_policy(self) -> ContentSafetyPolicy:
        settings = await self.settings()
        return ContentSafetyPolicy(
            match_mode=settings[MODE_KEY],
            custom_terms=tuple(settings[TERMS_KEY]),
            blacklisted_illust_ids=tuple(settings[IDS_KEY]),
        )

    async def update_global(self, payload: dict) -> dict:
        async with self._lock:
            settings = self._snapshot()
            if not set(payload).issubset({TERMS_KEY, IDS_KEY, MODE_KEY}):
                raise ValueError("未知的全局安全字段")
            settings.update(payload)
            mode = settings[MODE_KEY]
            if mode not in MATCH_MODES:
                raise ValueError("关键词匹配方式无效")
            settings[TERMS_KEY] = list(normalize_terms(settings[TERMS_KEY]))
            settings[IDS_KEY] = list(normalize_illust_ids(settings[IDS_KEY]))
            self._save({key: settings[key] for key in payload})
            return deepcopy(settings)

    async def change_global_item(self, key: str, value: str, *, remove=False) -> dict:
        async with self._lock:
            if key not in (TERMS_KEY, IDS_KEY):
                raise ValueError("未知的全局安全列表")
            normalize = normalize_terms if key == TERMS_KEY else normalize_illust_ids
            item = normalize([value])[0]
            items = self._snapshot()[key]
            if remove:
                identity = normalize_safety_text if key == TERMS_KEY else str
                items = [value for value in items if identity(value) != identity(item)]
            else:
                items = list(normalize([*items, item]))
            self._save({key: items})
            return {key: items}

    async def save_group(self, payload: dict) -> dict:
        async with self._lock:
            if not set(payload).issubset(
                {"group_id", "name", "sessions", "allow_r18", "custom_terms"}
            ):
                raise ValueError("未知的安全分组字段")
            groups = self._snapshot()[CONFIG_KEY]
            group_id = payload.get("group_id") or uuid.uuid4().hex
            existing = next(
                (group for group in groups if group["group_id"] == group_id), None
            )
            if payload.get("group_id") and existing is None:
                raise LookupError("安全分组不存在")
            item = {**(existing or {}), **payload, "group_id": group_id}
            candidate = [group for group in groups if group["group_id"] != group_id] + [
                item
            ]
            candidate = normalize_groups(candidate)
            self._save({CONFIG_KEY: candidate})
            return next(group for group in candidate if group["group_id"] == group_id)

    async def remove_group(self, group_id: str) -> None:
        async with self._lock:
            groups = self._snapshot()[CONFIG_KEY]
            if not any(group["group_id"] == group_id for group in groups):
                raise LookupError("安全分组不存在")
            self._save(
                {
                    CONFIG_KEY: [
                        group for group in groups if group["group_id"] != group_id
                    ]
                }
            )

    async def legacy_archives(self) -> list[dict]:
        if self.data_dir is None:
            raise RuntimeError("安全归档目录尚未初始化")

        def read():
            return [
                json.loads(path.read_text(encoding="utf-8"))
                for path in sorted(
                    (self.data_dir / "safety_legacy_backups").glob("legacy-*.json")
                )
            ]

        return await asyncio.to_thread(read)
