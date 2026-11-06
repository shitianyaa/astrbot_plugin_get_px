from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
import time
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageStat


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT.parent))

from astrbot.core import html_renderer  # noqa: E402
from astrbot_plugin_get_px.checkin.card import (  # noqa: E402
    CHECKIN_CARD_HEIGHT,
    CHECKIN_CARD_WIDTH,
    CardBackground,
    build_checkin_card_data,
    get_checkin_card_template,
)
from astrbot_plugin_get_px.checkin.quality import (  # noqa: E402
    CHECKIN_JPEG_QUALITY,
    CHECKIN_RENDER_TIERS,
)
from scripts.generate_checkin_event_matrix import make_profile, make_record  # noqa: E402


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "data" / "temp" / "t2i_quality_probe"


def _fixture_background(output_dir: Path) -> Path:
    path = output_dir / "source_fixture.png"
    image = Image.new("RGB", (750, 1000), "#d9b9a4")
    draw = ImageDraw.Draw(image)
    for y in range(image.height):
        color = (217 - y // 20, 185 - y // 35, 164 + y // 25)
        draw.line(
            (0, y, image.width, y),
            fill=tuple(max(0, min(255, channel)) for channel in color),
        )
    draw.ellipse((120, 170, 630, 680), fill="#f3dfcb", outline="#8e5a4a", width=8)
    draw.line((160, 760, 590, 760), fill="#8e5a4a", width=5)
    image.save(path)
    return path


def _render_data(background_path: Path) -> dict[str, Any]:
    scenario = type(
        "Scenario",
        (),
        {
            "date_key": "2026-07-20",
            "total_days": 12,
            "streak_days": 5,
            "affection": 66.6,
            "boost": False,
            "birthday": "",
            "custom_event": "",
            "secondary_events": (),
            "online_holiday": None,
            "username": "T2I 测试用户",
            "greeting_override": "",
            "with_artwork": True,
        },
    )()
    background = CardBackground(
        image_path=str(background_path),
        mode="pixiv_daily",
        source="fixture",
        illust_id="445566",
        title="夏日画页",
        author="测试画师",
        quality="large",
    )
    return build_checkin_card_data(
        profile=make_profile(scenario),
        record=make_record(scenario),
        bot_name="Neko",
        background=background,
        user_title="今日旅人",
    )


def _inspect_image(path: Path, expected_size: tuple[int, int]) -> dict[str, Any]:
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        grayscale = image.convert("L")
        edge_variance = ImageStat.Stat(grayscale.filter(ImageFilter.FIND_EDGES)).var[0]
        extrema = grayscale.getextrema()
        if image.format != "JPEG":
            raise RuntimeError(f"{path.name} is not JPEG: {image.format}")
        if image.size != expected_size:
            raise RuntimeError(
                f"{path.name} size mismatch: {image.size} != {expected_size}"
            )
        if extrema[0] == extrema[1] or edge_variance <= 1:
            raise RuntimeError(f"{path.name} appears blank")
        return {
            "format": image.format,
            "width": image.width,
            "height": image.height,
            "bytes": path.stat().st_size,
            "edge_variance": round(edge_variance, 3),
        }


async def run(output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    background_path = _fixture_background(output_dir)
    data = _render_data(background_path)
    report: list[dict[str, Any]] = []

    for index, spec in enumerate(CHECKIN_RENDER_TIERS.values(), 1):
        width, height = CHECKIN_CARD_WIDTH, CHECKIN_CARD_HEIGHT
        options: dict[str, Any] = {
            "full_page": False,
            "type": "jpeg",
            "quality": CHECKIN_JPEG_QUALITY,
            "clip": {"x": 0, "y": 0, "width": width, "height": height},
            "viewport": {"width": width, "height": height},
            "animations": "disabled",
        }
        if spec.scale_level is not None:
            options["device_scale_factor_level"] = spec.scale_level

        started = time.perf_counter()
        rendered = await html_renderer.render_custom_template(
            get_checkin_card_template(),
            data,
            return_url=False,
            options=options,
        )
        source = Path(str(rendered))
        destination = output_dir / f"{index}_{spec.scale_level or 'normal'}.jpg"
        try:
            shutil.copyfile(source, destination)
            info = _inspect_image(destination, spec.expected_size)
        finally:
            source.unlink(missing_ok=True)
        info.update(
            {
                "tier": spec.name,
                "background_quality": spec.background_quality,
                "scale_level": spec.scale_level or "normal",
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "path": str(destination),
            }
        )
        report.append(info)

    report_path = output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"output_dir": str(output_dir), "report": report}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = asyncio.run(run(args.output_dir.resolve()))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
