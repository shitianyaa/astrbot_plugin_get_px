"""代理配置、跨客户端分流与失败不直连的长期边界。"""

import asyncio
import importlib
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
from aiohttp_socks import ProxyConnector
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astrbot_plugin_get_px.pixiv.discovery import DiscoveryService
from astrbot_plugin_get_px.pixiv.network import (
    NETWORK_ERRORS, NetworkRouting, PROXY_ALL, PROXY_SKIP_LOLICON, create_http_session,
)

ROOT = Path(__file__).resolve().parents[1]
GetPxPlugin = importlib.import_module(f"{ROOT.name}.main").GetPxPlugin


@pytest.mark.asyncio
@pytest.mark.parametrize("url,mode", [
    ("", PROXY_ALL),
    ("http://user:pass@127.0.0.1:10808", PROXY_ALL),
    ("socks5h://127.0.0.1:10808", PROXY_SKIP_LOLICON),
])
async def test_plugin_routes_api_images_stats_and_greetings(url, mode, monkeypatch):
    from pixivpy_async import AppPixivAPI

    monkeypatch.setenv("https_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setattr(AppPixivAPI, "login", AsyncMock())
    plugin = GetPxPlugin(SimpleNamespace(), {
        "pixiv_source": {"pixiv_refresh_token": "fixture"},
        "runtime": {"network_proxy_url": url, "network_proxy_mode": mode},
    })
    plugin._init_client()
    routing = plugin.network_routing
    expected = url.replace("socks5h://", "socks5://")
    assert routing.proxy_url == expected
    assert "user:pass" not in repr(routing)
    assert plugin.client.proxy_url == expected
    await plugin.client.ensure_logged_in()
    assert plugin.client.api.conn_opt["proxy"] == (expected or None)
    assert plugin.client.api.conn_opt["env"] is False
    discovery = DiscoveryService(plugin)
    clients = [plugin.downloader, plugin.checkin_greeting, discovery.stats_client]
    try:
        for client in clients:
            assert client.proxy_url == expected
            session = client._ensure_session()
            assert session.trust_env is False
            assert isinstance(session.connector, ProxyConnector) is bool(expected)
        lolicon_session = plugin.lolicon_client._ensure_session()
        expected_lolicon = "" if mode == PROXY_SKIP_LOLICON else expected
        assert routing.lolicon_proxy_url == expected_lolicon
        assert plugin.lolicon_client.proxy_url == expected_lolicon
        assert isinstance(lolicon_session.connector, ProxyConnector) is bool(expected_lolicon)
        assert lolicon_session.trust_env is False
    finally:
        await discovery.stats_client.close()
        await plugin.client.close()
        await plugin.lolicon_client.close()
        await plugin.downloader.close()
        await plugin.checkin_greeting.close()
    assert all(client._session is None or client._session.closed for client in clients)


def test_invalid_proxy_config_never_becomes_direct_or_exposes_credentials():
    invalid = [
        "https://user:secret@localhost:10808", "socks4://localhost:10808",
        "http://localhost", "http://localhost:0", "http://localhost:65536",
        "http://localhost:10808/path", "http://localhost:10808?",
        "http://localhost:10808#", "http://local host:10808",
        "http://localhost:10808\nignored",
    ]
    for url in invalid:
        with pytest.raises(ValueError) as error:
            NetworkRouting(url)
        assert "secret" not in str(error.value)
    with pytest.raises(ValueError):
        NetworkRouting("", "invalid")
    assert NetworkRouting("http://[::1]:10808/").proxy_url == "http://[::1]:10808"


@pytest.mark.asyncio
async def test_proxy_failure_does_not_reach_direct_destination():
    direct_calls = []
    async def direct(reader, writer):
        direct_calls.append(await reader.read(1024))
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(direct, "127.0.0.1", 0)
    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        proxy_port = unavailable.getsockname()[1]
    try:
        async with create_http_session(f"http://127.0.0.1:{proxy_port}") as session:
            with pytest.raises(NETWORK_ERRORS):
                await session.get(
                    f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/",
                    timeout=aiohttp.ClientTimeout(total=1),
                )
        assert direct_calls == []
    finally:
        server.close()
        await server.wait_closed()
