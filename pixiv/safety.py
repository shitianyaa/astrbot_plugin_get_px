from __future__ import annotations

import unicodedata
from dataclasses import dataclass


MATCH_MODES = ("宽松", "严格")


def normalize_safety_text(value: object) -> str:
    """保留词内分隔符，避免不同标签被合并为同一个词。"""
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def normalize_terms(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("屏蔽关键词必须是列表")
    terms = {}
    for value in values:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
            raise ValueError("屏蔽关键词必须为 1–64 个字符")
        if any(ord(char) < 32 for char in value):
            raise ValueError("屏蔽关键词不能包含控制字符")
        terms.setdefault(normalize_safety_text(value), value.strip())
    return tuple(terms[key] for key in sorted(terms))


def normalize_illust_ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("作品黑名单必须是列表")
    ids = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value.strip().isascii()
            or not value.strip().isdigit()
            or int(value.strip()) <= 0
        ):
            raise ValueError("作品 ID 必须是正整数文本")
        ids.add(str(int(value.strip())))
    return tuple(sorted(ids, key=int))


@dataclass(frozen=True, slots=True)
class ContentSafetyPolicy:
    """一次请求的实际规则快照，不包含可切换的内置词或全局规则来源。"""

    general_only_enabled: bool = True
    match_mode: str = "宽松"
    custom_terms: tuple[str, ...] = ()
    blacklisted_illust_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if (
            type(self.general_only_enabled) is not bool
            or self.match_mode not in MATCH_MODES
        ):
            raise ValueError("内容安全开关或匹配方式无效")
        object.__setattr__(self, "custom_terms", normalize_terms(self.custom_terms))
        object.__setattr__(
            self,
            "blacklisted_illust_ids",
            normalize_illust_ids(self.blacklisted_illust_ids),
        )

    def cache_identity(self) -> dict[str, object]:
        # 身份只取实际规则；相同规则的不同会话可以安全复用卡片。
        return {
            "general_only_enabled": self.general_only_enabled,
            "match_mode": self.match_mode,
            "terms": sorted(normalize_safety_text(term) for term in self.custom_terms),
            "blacklisted_illust_ids": list(self.blacklisted_illust_ids),
        }


STRICT_CONTENT_SAFETY_POLICY = ContentSafetyPolicy()


def match_safety_term(value: object, terms: set[str] | frozenset[str]) -> str:
    """查询词按完整词匹配；不将输入拆词或执行正则。"""
    text = normalize_safety_text(value)
    return text if text in terms else ""


def illustration_texts(illust: dict) -> list[str]:
    """只列作品标签及翻译标签，不读取简介。"""
    values = []
    for tag in illust.get("tags") or []:
        if isinstance(tag, dict):
            values.extend(
                (str(tag.get("name") or ""), str(tag.get("translated_name") or ""))
            )
    return values


def matched_illustration_term(illust: dict, policy: ContentSafetyPolicy) -> str:
    terms = {normalize_safety_text(term) for term in policy.custom_terms}
    for value in illustration_texts(illust):
        if matched := match_safety_term(value, terms):
            return matched
    if policy.match_mode == "严格":
        title = normalize_safety_text(illust.get("title"))
        return next(
            (
                term
                for term in sorted(terms, key=lambda term: (-len(term), term))
                if term in title
            ),
            "",
        )
    return ""
