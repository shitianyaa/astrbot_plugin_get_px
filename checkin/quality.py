from __future__ import annotations

from dataclasses import dataclass


CHECKIN_RENDER_TIER_ECONOMY = "省流量"
CHECKIN_RENDER_TIER_CLEAR = "清晰"
CHECKIN_RENDER_TIER_ULTIMATE = "极致"
DEFAULT_CHECKIN_RENDER_TIER = CHECKIN_RENDER_TIER_ECONOMY
CHECKIN_JPEG_QUALITY = 95


@dataclass(frozen=True)
class CheckinRenderTier:
    name: str
    background_quality: str
    scale_level: str | None
    expected_size: tuple[int, int]


CHECKIN_RENDER_TIERS: dict[str, CheckinRenderTier] = {
    CHECKIN_RENDER_TIER_ECONOMY: CheckinRenderTier(
        name=CHECKIN_RENDER_TIER_ECONOMY,
        background_quality="medium",
        scale_level=None,
        expected_size=(960, 540),
    ),
    CHECKIN_RENDER_TIER_CLEAR: CheckinRenderTier(
        name=CHECKIN_RENDER_TIER_CLEAR,
        background_quality="large",
        scale_level="high",
        expected_size=(1248, 702),
    ),
    CHECKIN_RENDER_TIER_ULTIMATE: CheckinRenderTier(
        name=CHECKIN_RENDER_TIER_ULTIMATE,
        background_quality="large",
        scale_level="ultra",
        expected_size=(1728, 972),
    ),
}

_TIER_ALIASES = {
    "economy": CHECKIN_RENDER_TIER_ECONOMY,
    "low": CHECKIN_RENDER_TIER_ECONOMY,
    "normal": CHECKIN_RENDER_TIER_ECONOMY,
    "clear": CHECKIN_RENDER_TIER_CLEAR,
    "high": CHECKIN_RENDER_TIER_CLEAR,
    "ultimate": CHECKIN_RENDER_TIER_ULTIMATE,
    "ultra": CHECKIN_RENDER_TIER_ULTIMATE,
}

_FALLBACK_ORDER = (
    CHECKIN_RENDER_TIER_ULTIMATE,
    CHECKIN_RENDER_TIER_CLEAR,
    CHECKIN_RENDER_TIER_ECONOMY,
)


def normalize_checkin_render_tier(value: object) -> str:
    raw = str(value or "").strip()
    if raw in CHECKIN_RENDER_TIERS:
        return raw
    return _TIER_ALIASES.get(raw.lower(), DEFAULT_CHECKIN_RENDER_TIER)


def validate_checkin_render_tier(value: object) -> str:
    raw = str(value or "").strip()
    if raw not in CHECKIN_RENDER_TIERS:
        raise ValueError(f"invalid check-in render tier: {raw!r}")
    return raw


def get_checkin_render_tier(value: object) -> CheckinRenderTier:
    return CHECKIN_RENDER_TIERS[normalize_checkin_render_tier(value)]


def checkin_render_fallbacks(value: object) -> tuple[CheckinRenderTier, ...]:
    normalized = normalize_checkin_render_tier(value)
    start = _FALLBACK_ORDER.index(normalized)
    return tuple(CHECKIN_RENDER_TIERS[name] for name in _FALLBACK_ORDER[start:])
