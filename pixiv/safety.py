from __future__ import annotations

import re
import unicodedata


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
