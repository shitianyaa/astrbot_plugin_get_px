from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks4", "socks5"})
PROXY_SCHEME_ALIASES = {"socks4a": "socks4", "socks5h": "socks5"}
DEFAULT_IMAGE_REVERSE_PROXY_HOST = "i.pixiv.re"
PIXIV_IMAGE_HOSTS = frozenset(
    {"i.pximg.net", "i.pixiv.re", "i.pixiv.cat", "proxy.pixivel.moe"}
)


def normalize_proxy_url(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""

    try:
        parsed = urlsplit(value)
        raw_scheme = parsed.scheme.casefold()
        scheme = PROXY_SCHEME_ALIASES.get(raw_scheme, raw_scheme)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理地址格式无效") from exc

    if scheme not in SUPPORTED_PROXY_SCHEMES:
        raise ValueError("代理协议仅支持 http、https、socks4、socks4a、socks5 或 socks5h")
    if not parsed.hostname or port is None:
        raise ValueError("代理地址必须包含主机和端口")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")

    return urlunsplit((scheme, parsed.netloc, "", "", ""))


def is_socks_proxy(proxy: str) -> bool:
    return urlsplit(proxy).scheme in {"socks4", "socks5"}


def resolve_pixiv_image_proxy_host(api_proxy: str, configured_host: str) -> str:
    custom_host = str(configured_host or "").strip()
    if custom_host:
        return custom_host
    if str(api_proxy or "").strip():
        return DEFAULT_IMAGE_REVERSE_PROXY_HOST
    return ""


def is_pixiv_image_url(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").casefold() in PIXIV_IMAGE_HOSTS
    except ValueError:
        return False


def rewrite_pixiv_image_url(url: str, reverse_proxy: str) -> str:
    value = str(reverse_proxy or "").strip()
    if not value or not is_pixiv_image_url(url):
        return url

    try:
        target = urlsplit(value if "://" in value else f"//{value}")
    except ValueError as exc:
        raise ValueError("图片反代地址格式无效") from exc
    if target.scheme and target.scheme not in {"http", "https"}:
        raise ValueError("图片反代协议仅支持 http 或 https")
    if not target.netloc or not target.hostname:
        raise ValueError("图片反代地址必须包含主机")
    if target.path not in ("", "/") or target.query or target.fragment:
        raise ValueError("图片反代地址不能包含路径、查询参数或片段")

    source = urlsplit(url)
    return urlunsplit(
        (
            target.scheme or source.scheme or "https",
            target.netloc,
            source.path,
            source.query,
            source.fragment,
        )
    )
