from __future__ import annotations


def parse_aspect_ratio(value: object) -> float:
    raw = str(value or "").strip().lower()
    if not raw:
        return 0.0

    normalized = (
        raw.replace("：", ":")
        .replace("／", "/")
        .replace("×", "x")
        .replace(" ", "")
    )
    for separator in (":", "/", "x"):
        if separator not in normalized:
            continue
        parts = normalized.split(separator)
        if len(parts) != 2:
            return 0.0
        return _ratio_from_numbers(parts[0], parts[1])

    try:
        ratio = float(normalized)
    except (TypeError, ValueError):
        return 0.0
    return ratio if ratio > 0 else 0.0


def filter_illusts_by_aspect_ratio(
    illusts: list[dict],
    target_ratio: float,
    tolerance: float,
) -> list[dict]:
    if target_ratio <= 0:
        return list(illusts)
    return [
        illust
        for illust in illusts
        if aspect_ratio_matches(illust, target_ratio, tolerance)
    ]


def aspect_ratio_matches(illust: dict, target_ratio: float, tolerance: float) -> bool:
    if target_ratio <= 0:
        return True
    actual_ratio = illust_aspect_ratio(illust)
    if actual_ratio <= 0:
        return False
    allowed_delta = target_ratio * max(0.0, tolerance)
    return abs(actual_ratio - target_ratio) <= allowed_delta


def illust_aspect_ratio(illust: dict) -> float:
    width, height = illust_dimensions(illust)
    if width <= 0 or height <= 0:
        return 0.0
    return width / height


def illust_dimensions(illust: dict) -> tuple[int, int]:
    width = _positive_int(illust.get("width"))
    height = _positive_int(illust.get("height"))
    if width > 0 and height > 0:
        return width, height

    meta_pages = illust.get("meta_pages") or []
    if meta_pages:
        first_page = meta_pages[0] or {}
        image_urls = first_page.get("image_urls") or {}
        width = _positive_int(first_page.get("width") or image_urls.get("width"))
        height = _positive_int(first_page.get("height") or image_urls.get("height"))
        if width > 0 and height > 0:
            return width, height

    return 0, 0


def _ratio_from_numbers(width_text: str, height_text: str) -> float:
    try:
        width = float(width_text)
        height = float(height_text)
    except (TypeError, ValueError):
        return 0.0
    if width <= 0 or height <= 0:
        return 0.0
    return width / height


def _positive_int(value: object) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0
