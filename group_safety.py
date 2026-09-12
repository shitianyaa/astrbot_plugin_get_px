from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

# AstrBot loads this plugin as a package, while the repository tests also
# import modules directly from the checkout root. Keep both contexts explicit
# so a failed package import never falls through to a top-level fallback.
if __package__:
    from .pixiv.safety import normalize_safety_text, normalize_safety_term_identity
else:
    from pixiv.safety import normalize_safety_text, normalize_safety_term_identity

try:
    from astrbot.api.all import logger
except ImportError:
    import logging

    logger = logging.getLogger(__name__)

CONFIG_KEY = "group_content_safety_policies"
PRIVATE_CONFIG_KEY = "private_content_safety_policies"
MIGRATION_KEY = "group_content_safety_policies_migrated"
TEMPLATE_KEY = "group_policy"
PRIVATE_TEMPLATE_KEY = "private_policy"
MAX_GROUP_ID_LENGTH = 128
LIST_FIELDS = ("custom_terms", "blacklisted_illust_ids")


def _normalize_terms(value: object, *, strict: bool = True, field: str = "custom_terms") -> list[str]:
    if not isinstance(value, list):
        if strict:
            raise ValueError(f"{field} must be a list")
        return []
    result = {}
    for index, item in enumerate(value):
        if not isinstance(item, str):
            if strict:
                raise ValueError(f"{field}[{index}] must be a string")
            continue
        display = item.strip()
        normalized = normalize_safety_text(display)
        if not normalized:
            if strict:
                raise ValueError(f"{field}[{index}] is required")
            continue
        result.setdefault(normalize_safety_term_identity(display), display)
    return [result[key] for key in sorted(result, key=lambda k: (k, result[k]))]


def _normalize_ids(value: object, *, strict: bool = True, field: str = "blacklisted_illust_ids") -> list[str]:
    if not isinstance(value, list):
        if strict:
            raise ValueError(f"{field} must be a list")
        return []
    result = set()
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or not item.strip().isdigit() or int(item.strip()) <= 0:
            if strict:
                raise ValueError(f"{field}[{index}] must be a positive integer string")
            continue
        result.add(str(int(item.strip())))
    return sorted(result, key=int)


def _normalize_lists(item: dict, *, strict: bool = True) -> tuple[list[str], list[str]]:
    return (
        _normalize_terms(item.get("custom_terms", []), strict=strict),
        _normalize_ids(item.get("blacklisted_illust_ids", []), strict=strict),
    )


def _normalize(value: object, key: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{key} is required")
    if len(value) > MAX_GROUP_ID_LENGTH:
        raise ValueError(f"{key} must not exceed {MAX_GROUP_ID_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{key} contains invalid control characters")
    return value


def normalize_group_id(value: object) -> str:
    return _normalize(value, "group_id")


def normalize_user_id(value: object) -> str:
    return _normalize(value, "user_id")


def strict_policy(identifier: str, *, private: bool = False) -> dict[str, object]:
    return {
        "user_id" if private else "group_id": identifier,
        "general_only_enabled": True,
        "builtin_terms_enabled": True,
        "updated_by": "",
        "updated_at": "",
        "is_default": True,
        "custom_terms": [],
        "blacklisted_illust_ids": [],
    }


def _normalize_entries(raw: object, *, private: bool = False):
    key = "user_id" if private else "group_id"
    template = PRIVATE_TEMPLATE_KEY if private else TEMPLATE_KEY
    normalize_id = normalize_user_id if private else normalize_group_id
    if not isinstance(raw, list):
        return [], [f"{key} policy list must be a list"]
    entries, warnings, seen = [], [], set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"entry {index}: must be an object")
            continue
        try:
            identifier = normalize_id(item.get(key))
            if type(item.get("general_only_enabled")) is not bool:
                raise ValueError("general_only_enabled must be a boolean")
            if type(item.get("builtin_terms_enabled")) is not bool:
                raise ValueError("builtin_terms_enabled must be a boolean")
            custom_terms, blacklisted_ids = _normalize_lists(item, strict=False)
            raw_terms = item.get("custom_terms", [])
            raw_ids = item.get("blacklisted_illust_ids", [])
            if isinstance(raw_terms, list):
                warnings.extend(f"entry {index}: custom_terms[{i}] invalid and dropped" for i, v in enumerate(raw_terms) if not isinstance(v, str) or not v.strip() or not normalize_safety_text(v.strip()))
            elif "custom_terms" in item:
                warnings.append(f"entry {index}: custom_terms must be a list; dropped")
            if isinstance(raw_ids, list):
                warnings.extend(f"entry {index}: blacklisted_illust_ids[{i}] invalid and dropped" for i, v in enumerate(raw_ids) if not isinstance(v, str) or not v.strip().isdigit() or int(v.strip()) <= 0)
            elif "blacklisted_illust_ids" in item:
                warnings.append(f"entry {index}: blacklisted_illust_ids must be a list; dropped")
        except ValueError as exc:
            warnings.append(f"entry {index}: {exc}")
            continue
        if identifier in seen:
            warnings.append(f"entry {index}: duplicate {key} {identifier}")
            continue
        seen.add(identifier)
        entries.append(
            {
                "__template_key": template,
                key: identifier,
                "general_only_enabled": item["general_only_enabled"],
                "builtin_terms_enabled": item["builtin_terms_enabled"],
                "updated_by": str(item.get("updated_by") or ""),
                "updated_at": str(item.get("updated_at") or ""),
                "custom_terms": custom_terms,
                "blacklisted_illust_ids": blacklisted_ids,
            }
        )
    return sorted(entries, key=lambda entry: entry[key]), warnings


def normalize_policy_entries(raw: object):
    return _normalize_entries(raw)


class SessionSafetyService:
    def __init__(self, config: Any, *, log_prefix: str = "[GetPx]"):
        self.config = config
        self.log_prefix = log_prefix
        self._lock = asyncio.Lock()
        self._policies: dict[str, dict[str, object]] = {}
        self._private_policies: dict[str, dict[str, object]] = {}

    @staticmethod
    def _scope(private: bool):
        return (
            (PRIVATE_CONFIG_KEY, "user_id", PRIVATE_TEMPLATE_KEY)
            if private
            else (CONFIG_KEY, "group_id", TEMPLATE_KEY)
        )

    def _source(self, private: bool):
        return self._private_policies if private else self._policies

    def _records(self, entries, *, private: bool):
        _, key, _ = self._scope(private)
        return {
            str(entry[key]): {
                key: str(entry[key]),
                "general_only_enabled": entry["general_only_enabled"],
                "builtin_terms_enabled": entry["builtin_terms_enabled"],
                "updated_by": str(entry.get("updated_by") or ""),
                "updated_at": str(entry.get("updated_at") or ""),
                "is_default": False,
                "custom_terms": list(entry.get("custom_terms", [])),
                "blacklisted_illust_ids": list(entry.get("blacklisted_illust_ids", [])),
            }
            for entry in entries
        }

    def _install(self, entries, *, private: bool = False):
        target = self._source(private)
        target.clear()
        target.update(self._records(entries, private=private))

    def _entries(self, records, *, private: bool):
        _, key, template = self._scope(private)
        return [
            {
                "__template_key": template,
                key: identifier,
                "general_only_enabled": record["general_only_enabled"],
                "builtin_terms_enabled": record["builtin_terms_enabled"],
                "updated_by": str(record.get("updated_by") or ""),
                "updated_at": str(record.get("updated_at") or ""),
                "custom_terms": list(record.get("custom_terms", [])),
                "blacklisted_illust_ids": list(record.get("blacklisted_illust_ids", [])),
            }
            for identifier, record in sorted(records.items())
        ]

    def _container(self) -> dict:
        """Return the grouped config container while keeping the root config as saver."""
        grouped = self.config.get("content_dedupe")
        return grouped if isinstance(grouped, dict) else self.config

    def _get_config(self, key: str, default=None):
        container = self._container()
        if key in container:
            nested = container.get(key, default)
            # AstrBot may materialize invisible flat compatibility defaults.
            # A non-empty legacy value still needs one-time migration into the
            # grouped container when the nested template is empty.
            if container is not self.config and not nested:
                legacy = self.config.get(key, default)
                if legacy:
                    return legacy
            return nested
        return self.config.get(key, default)

    def _set_config(self, key: str, value) -> None:
        self._container()[key] = value

    def _snapshot_config(self):
        container = self._container()
        return (
            container,
            deepcopy(container),
            {key: (key in self.config, self.config.get(key),
                   deepcopy(self.config.get(key))) for key in
             (CONFIG_KEY, PRIVATE_CONFIG_KEY, MIGRATION_KEY)},
        )

    def _restore_config(self, snapshot) -> None:
        container, container_copy, root_snapshot = snapshot
        existing_refs = {key: self.config.get(key) for key in
                        (CONFIG_KEY, PRIVATE_CONFIG_KEY, MIGRATION_KEY)}
        nested_refs = {key: container.get(key) for key in
                       (CONFIG_KEY, PRIVATE_CONFIG_KEY, MIGRATION_KEY)}
        restored = deepcopy(container_copy)
        refs = nested_refs if container is not self.config else existing_refs
        for key, ref in refs.items():
            if isinstance(ref, list) and key in restored:
                ref[:] = deepcopy(restored[key])
                restored[key] = ref
        container.clear()
        container.update(restored)
        if container is self.config:
            return
        for key, (present, value, value_copy) in root_snapshot.items():
            if present:
                current = self.config.get(key)
                if isinstance(current, list) and isinstance(value, list):
                    current[:] = deepcopy(value_copy)
                    self.config[key] = current
                else:
                    self.config[key] = deepcopy(value_copy)
            else:
                self.config.pop(key, None)

    def _save_scope(self, *, private: bool, candidate, migrated=None):
        config_key, _, _ = self._scope(private)
        snapshot = self._snapshot_config()
        serialized = self._entries(candidate, private=private)
        current = self._container().get(config_key)
        if isinstance(current, list):
            current[:] = deepcopy(serialized)
        else:
            self._set_config(config_key, deepcopy(serialized))
        if not private and migrated is not None:
            self._set_config(MIGRATION_KEY, bool(migrated))
        try:
            saver = getattr(self.config, "save_config", None)
            if not callable(saver):
                raise RuntimeError("config.save_config is required")
            saver()
        except Exception:
            self._restore_config(snapshot)
            raise

    def _save_candidates(self, group_candidate=None, private_candidate=None, migrated=None):
        snapshot = self._snapshot_config()
        try:
            if group_candidate is not None:
                serialized = self._entries(group_candidate, private=False)
                current = self._container().get(CONFIG_KEY)
                if isinstance(current, list):
                    current[:] = deepcopy(serialized)
                else:
                    self._set_config(CONFIG_KEY, deepcopy(serialized))
            if private_candidate is not None:
                serialized = self._entries(private_candidate, private=True)
                current = self._container().get(PRIVATE_CONFIG_KEY)
                if isinstance(current, list):
                    current[:] = deepcopy(serialized)
                else:
                    self._set_config(PRIVATE_CONFIG_KEY, deepcopy(serialized))
            if migrated is not None:
                self._set_config(MIGRATION_KEY, bool(migrated))
            saver = getattr(self.config, "save_config", None)
            if not callable(saver):
                raise RuntimeError("config.save_config is required")
            saver()
        except Exception:
            self._restore_config(snapshot)
            raise

    def _log_warnings(self, source: str, warnings):
        for warning in warnings:
            logger.warning(f"{self.log_prefix} {source}: {warning}")

    async def initialize(self, legacy_store):
        async with self._lock:
            migration_succeeded = True
            raw_private = self._get_config(PRIVATE_CONFIG_KEY, [])
            private_entries, warnings = _normalize_entries(raw_private, private=True)
            self._log_warnings("private config", warnings)
            self._install(private_entries, private=True)
            if raw_private != private_entries:
                try:
                    self._save_scope(
                        private=True, candidate=deepcopy(self._private_policies)
                    )
                except Exception as exc:
                    self._install([], private=True)
                    migration_succeeded = False
                    self._log_warnings(
                        "private config",
                        [f"save failed error_type={type(exc).__name__}"],
                    )

            raw_group = self._get_config(CONFIG_KEY, [])
            group_entries, warnings = _normalize_entries(raw_group)
            self._log_warnings("config", warnings)
            migrated = bool(self._get_config(MIGRATION_KEY, False))
            use_legacy = not migrated and isinstance(raw_group, list) and not raw_group
            if use_legacy:
                try:
                    group_entries, warnings = _normalize_entries(
                        await legacy_store.list_group_content_safety_records()
                    )
                    self._log_warnings("legacy", warnings)
                except Exception as exc:
                    self._install([], private=False)
                    migration_succeeded = False
                    self._log_warnings(
                        "legacy", [f"read failed error_type={type(exc).__name__}"]
                    )
                    return False
            self._install(group_entries, private=False)
            if isinstance(raw_group, list) and migrated and raw_group == group_entries:
                return migration_succeeded
            try:
                self._save_scope(
                    private=False,
                    candidate=deepcopy(self._policies),
                    migrated=True,
                )
            except Exception as exc:
                self._install([], private=False)
                migration_succeeded = False
                self._log_warnings(
                    "legacy" if use_legacy else "config",
                    [f"save failed error_type={type(exc).__name__}"],
                )
            return migration_succeeded

    async def _list(self, *, private: bool = False):
        async with self._lock:
            return [deepcopy(record) for _, record in sorted(self._source(private).items())]

    async def _get(self, identifier, *, private: bool = False):
        _, key, _ = self._scope(private)
        identifier = _normalize(identifier, key)
        async with self._lock:
            return deepcopy(
                self._source(private).get(
                    identifier, strict_policy(identifier, private=private)
                )
            )

    async def _upsert(
        self,
        identifier,
        *,
        general_only_enabled,
        builtin_terms_enabled,
        updated_by="",
        private=False,
        custom_terms=None,
        blacklisted_illust_ids=None,
    ):
        _, key, _ = self._scope(private)
        identifier = _normalize(identifier, key)
        if (
            type(general_only_enabled) is not bool
            or type(builtin_terms_enabled) is not bool
        ):
            raise ValueError("policy values must be booleans")
        async with self._lock:
            candidate = deepcopy(self._source(private))
            existing = candidate.get(identifier)
            if custom_terms is None:
                normalized_terms = list(existing.get("custom_terms", [])) if existing else []
            else:
                normalized_terms = _normalize_terms(custom_terms)
            if blacklisted_illust_ids is None:
                normalized_ids = list(existing.get("blacklisted_illust_ids", [])) if existing else []
            else:
                normalized_ids = _normalize_ids(blacklisted_illust_ids)
            candidate[identifier] = {
                key: identifier,
                "general_only_enabled": general_only_enabled,
                "builtin_terms_enabled": builtin_terms_enabled,
                "updated_by": str(updated_by or ""),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "is_default": False,
                "custom_terms": normalized_terms,
                "blacklisted_illust_ids": normalized_ids,
            }
            self._save_scope(private=private, candidate=candidate, migrated=True)
            target = self._source(private)
            target.clear()
            target.update(candidate)
            return dict(target[identifier])

    async def _remove(self, identifier, *, private=False):
        _, key, _ = self._scope(private)
        identifier = _normalize(identifier, key)
        async with self._lock:
            source = self._source(private)
            if identifier not in source:
                return False, strict_policy(identifier, private=private)
            candidate = deepcopy(source)
            del candidate[identifier]
            self._save_scope(private=private, candidate=candidate, migrated=True)
            source.clear()
            source.update(candidate)
            return True, strict_policy(identifier, private=private)

    async def list_group_policies(self):
        return await self._list()

    async def get_group_policy(self, group_id):
        return await self._get(group_id)

    async def upsert_group_policy(self, group_id, **kwargs):
        return await self._upsert(group_id, **kwargs)

    async def remove_group_policy(self, group_id):
        return await self._remove(group_id)

    async def list_private_policies(self):
        return await self._list(private=True)

    async def get_private_policy(self, user_id):
        return await self._get(user_id, private=True)

    async def upsert_private_policy(self, user_id, **kwargs):
        return await self._upsert(user_id, private=True, **kwargs)

    async def remove_private_policy(self, user_id):
        return await self._remove(user_id, private=True)

    async def apply_policy_field(self, source_scope, source_id, field, target, updated_by="web"):
        if source_scope not in {"group", "private"}:
            raise ValueError("invalid source_scope")
        if field not in LIST_FIELDS:
            raise ValueError("invalid field")
        if target not in {"group", "private", "all"}:
            raise ValueError("invalid target")
        source_private = source_scope == "private"
        source_id = _normalize(source_id, "user_id" if source_private else "group_id")
        async with self._lock:
            source = self._source(source_private)
            if source_id not in source:
                raise LookupError("source policy not found")
            value = list(source[source_id].get(field, []))
            group_candidate = deepcopy(self._policies) if target in {"group", "all"} else None
            private_candidate = deepcopy(self._private_policies) if target in {"private", "all"} else None
            changed_group = changed_private = 0
            for candidate, count in ((group_candidate, "group"), (private_candidate, "private")):
                if candidate is None:
                    continue
                for identifier, record in candidate.items():
                    if record.get(field, []) != value:
                        record[field] = list(value)
                        record["updated_by"] = str(updated_by or "")
                        record["updated_at"] = datetime.now(timezone.utc).isoformat()
                        if count == "group":
                            changed_group += 1
                        else:
                            changed_private += 1
            if not changed_group and not changed_private:
                return {"field": field, "target": target, "updated_count": 0, "group_updated_count": 0, "private_updated_count": 0}
            self._save_candidates(group_candidate, private_candidate, migrated=True if group_candidate is not None else None)
            if group_candidate is not None:
                self._policies = group_candidate
            if private_candidate is not None:
                self._private_policies = private_candidate
            return {"field": field, "target": target, "updated_count": changed_group + changed_private, "group_updated_count": changed_group, "private_updated_count": changed_private}

    async def list_policies(self):
        return await self.list_group_policies()

    async def get_policy(self, group_id):
        return await self.get_group_policy(group_id)

    async def upsert_policy(self, group_id, **kwargs):
        return await self.upsert_group_policy(group_id, **kwargs)

    async def remove_policy(self, group_id):
        return await self.remove_group_policy(group_id)


GroupSafetyService = SessionSafetyService
