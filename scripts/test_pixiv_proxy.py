from __future__ import annotations

import asyncio
import getpass
import hashlib
import os
import shutil
import sys
from pathlib import Path

from PIL import Image


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
FORMAT_SUFFIXES = {"JPEG": ".jpg", "PNG": ".png", "GIF": ".gif", "WEBP": ".webp"}
sys.path.insert(0, str(PLUGIN_ROOT.parent))

from astrbot_plugin_get_px.pixiv.client import PixivClient  # noqa: E402
from astrbot_plugin_get_px.pixiv.downloader import (  # noqa: E402
    ImageDownloader,
    cleanup,
    pick_image_url,
)
from astrbot_plugin_get_px.pixiv.proxy import (  # noqa: E402
    normalize_proxy_url,
    resolve_pixiv_image_proxy_host,
)


def _read_proxy() -> str:
    value = input("SOCKS5/HTTP proxy: ").strip()
    if value and "://" not in value:
        value = f"socks5h://{value}"
    return normalize_proxy_url(value)


def _read_refresh_token() -> str:
    token = os.environ.get("PIXIV_REFRESH_TOKEN", "").strip()
    return token or getpass.getpass("Pixiv refresh token: ").strip()


def _pick_safe_illust(illusts: list[dict]) -> dict | None:
    for illust in illusts:
        if (
            int(illust.get("x_restrict") or 0) == 0
            and int(illust.get("sanity_level") or 0) <= 4
            and illust.get("type") == "illust"
            and pick_image_url(illust, "large")
        ):
            return illust
    return None


async def _download_one(proxy: str, refresh_token: str) -> Path:
    client = PixivClient(refresh_token, proxy=proxy, request_timeout=45.0)
    downloader = ImageDownloader()
    image_reverse_proxy = resolve_pixiv_image_proxy_host(proxy, "")
    temp_path = ""
    try:
        illusts = await client.search("風景")
        illust = _pick_safe_illust(illusts)
        if illust is None:
            raise RuntimeError("没有找到可下载的普通分级作品")

        image_url = pick_image_url(illust, "large")
        temp_path = await downloader.download(
            image_url,
            proxy=proxy,
            timeout=60.0,
            reverse_proxy_host=image_reverse_proxy,
        )
        with Image.open(temp_path) as image:
            image.verify()
        with Image.open(temp_path) as image:
            image_format = image.format or "unknown"
            width, height = image.size

        payload_hash = hashlib.sha256(Path(temp_path).read_bytes()).hexdigest()
        output_dir = PLUGIN_ROOT / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = FORMAT_SUFFIXES.get(image_format.upper(), Path(temp_path).suffix)
        output_path = output_dir / f"pixiv_proxy_test_{illust['id']}{suffix}"
        shutil.move(temp_path, output_path)
        temp_path = ""
        print(
            f"Downloaded: id={illust['id']} format={image_format} "
            f"size={width}x{height} sha256={payload_hash}"
        )
        return output_path
    finally:
        cleanup(temp_path)
        await downloader.close()
        await client.close()


async def main() -> int:
    try:
        proxy = _read_proxy()
        refresh_token = _read_refresh_token()
        if not proxy:
            raise ValueError("代理地址不能为空")
        if not refresh_token:
            raise ValueError("refresh token 不能为空")
        output_path = await _download_one(proxy, refresh_token)
    except ValueError as exc:
        print(f"Invalid input: {exc}")
        return 2
    except Exception as exc:
        print(f"Download failed: {type(exc).__name__}")
        return 1
    finally:
        if "refresh_token" in locals():
            refresh_token = ""

    print(f"Saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
