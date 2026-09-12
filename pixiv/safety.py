from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ContentSafetyPolicy:
    """Immutable per-request content-safety policy snapshot."""

    general_only_enabled: bool = True
    builtin_terms_enabled: bool = True
    group_id: str = ""
    user_id: str = ""
    custom_terms: tuple[str, ...] = ()
    blacklisted_illust_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.custom_terms, (list, tuple)) or not isinstance(self.blacklisted_illust_ids, (list, tuple)):
            raise ValueError("independent safety lists must be arrays")
        terms_by_key = {}
        for index, value in enumerate(self.custom_terms):
            if not isinstance(value, str) or not value.strip() or not normalize_safety_text(value.strip()):
                raise ValueError(f"custom_terms[{index}] is invalid")
            display = value.strip()
            terms_by_key.setdefault(normalize_safety_term_identity(display), display)
        terms = [terms_by_key[key] for key in sorted(terms_by_key, key=lambda k: (k, terms_by_key[k]))]
        ids = set()
        for index, value in enumerate(self.blacklisted_illust_ids):
            if not isinstance(value, str) or not value.strip().isdigit() or int(value.strip()) <= 0:
                raise ValueError(f"blacklisted_illust_ids[{index}] is invalid")
            ids.add(str(int(value.strip())))
        ids = sorted(ids, key=int)
        object.__setattr__(self, "custom_terms", tuple(terms))
        object.__setattr__(self, "blacklisted_illust_ids", tuple(ids))

    def cache_identity(self) -> dict[str, object]:
        return {
            "scope": "group" if self.group_id else "private" if self.user_id else "default",
            "group_id": self.group_id,
            "user_id": self.user_id,
            "general_only_enabled": self.general_only_enabled,
            "builtin_terms_enabled": self.builtin_terms_enabled,
            "custom_terms": list(self.custom_terms),
            "blacklisted_illust_ids": list(self.blacklisted_illust_ids),
        }


STRICT_CONTENT_SAFETY_POLICY = ContentSafetyPolicy()


BUILTIN_SAFETY_TERMS = (
    "r18",
    "r-18",
    "r18g",
    "r-18g",
    "nsfw",
    "裸体",
    "全裸",
    "裸露",
    "露出",
    "成人",
    "色情",
    "性交",
    "性爱",
    "性器",
    "乳首",
    "乳房",
    "触手",
    "猎奇",
    "血腥",
    "断肢",
    "肢解",
    "内脏",
    "尸体",
    "腐烂",
    "虐杀",
    "guro",
    "gore",
    "grotesque",
    "グロ",
    "グロテスク",
    "リョナ",
    "猟奇",
    "欠損",
    "切断",
    "内臓",
    "死体",
)


def normalize_safety_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s_\-‐‑‒–—―·・.]+", "", text)


def normalize_safety_term_identity(value: object) -> str:
    """Normalize a stored term for duplicate identity without dropping punctuation."""
    return unicodedata.normalize("NFKC", str(value or "").strip()).casefold()


def normalized_builtin_terms() -> frozenset[str]:
    return frozenset(
        normalized
        for term in BUILTIN_SAFETY_TERMS
        if (normalized := normalize_safety_text(term))
    )


def match_safety_term(value: object, terms: set[str] | frozenset[str]) -> str:
    normalized = normalize_safety_text(value)
    if not normalized:
        return ""
    return next(
        (term for term in sorted(terms, key=len, reverse=True) if term in normalized),
        "",
    )


def illustration_texts(illust: dict) -> list[str]:
    values = [
        str(illust.get("title") or ""),
        str(illust.get("caption") or illust.get("description") or ""),
    ]
    for tag in illust.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        values.extend(
            (str(tag.get("name") or ""), str(tag.get("translated_name") or ""))
        )
    return values
