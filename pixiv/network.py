"""插件出站代理配置与独立 HTTP 会话；不读取或修改环境代理。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

NETWORK_ERRORS = (
    aiohttp.ClientError, TimeoutError, asyncio.TimeoutError,
    ProxyConnectionError, ProxyError, ProxyTimeoutError,
)
PROXY_ALL = "全部走代理"
PROXY_SKIP_LOLICON = "Lolicon API 不走代理"


def normalize_proxy_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.casefold()
        if scheme == "socks5h":
            scheme = "socks5"
        if (
            scheme not in {"http", "socks5"}
            or not parsed.hostname
            or parsed.port is None
            or not 1 <= parsed.port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or "\\" in value
            or any(char.isspace() or ord(char) < 32 for char in value)
        ):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError(
            "代理地址须使用 http://、socks5:// 或 socks5h://，包含主机和端口，不含路径、查询或片段"
        ) from None
    return urlunsplit((scheme, parsed.netloc, "", "", ""))


@dataclass(frozen=True)
class NetworkRouting:
    proxy_url: str = field(default="", repr=False)
    mode: str = PROXY_ALL

    def __post_init__(self) -> None:
        object.__setattr__(self, "proxy_url", normalize_proxy_url(self.proxy_url))
        if self.mode not in {PROXY_ALL, PROXY_SKIP_LOLICON}:
            raise ValueError("代理模式须为全部走代理或 Lolicon API 不走代理")

    @property
    def lolicon_proxy_url(self) -> str:
        return "" if self.mode == PROXY_SKIP_LOLICON else self.proxy_url


def create_http_session(proxy_url: str = "", **kwargs) -> aiohttp.ClientSession:
    proxy_url = normalize_proxy_url(proxy_url)
    if proxy_url:
        from aiohttp_socks import ProxyConnector

        # 每个客户端独占会话；SOCKS5 使用代理端 DNS，失败不回退直连。
        kwargs["connector"] = ProxyConnector.from_url(proxy_url, rdns=True)
    return aiohttp.ClientSession(trust_env=False, **kwargs)
