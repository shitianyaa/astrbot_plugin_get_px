# Pixiv 网络路由

Lolicon 负责首选作品数据，Pixiv refresh token 仅用于回退，API 出口代理与图片反代独立生效。

## 数据源顺序

1. `lolicon_api_url` 可用时先请求 Lolicon API。
2. Lolicon 返回非空结果时不调用 Pixiv。
3. Lolicon 失败或返回空结果时，存在 `pixiv_refresh_token` 才回退 Pixiv。
4. 有标签时调用 Pixiv 搜索，无标签时调用 Pixiv 推荐。

## 网络路径

| 请求 | 路由 | 凭证 |
| --- | --- | --- |
| Lolicon API | 本机直连 | 无 |
| Pixiv OAuth/API | `pixiv_proxy_url`；留空时直连 | refresh token 仅发送给官方 Pixiv OAuth |
| Pixiv 图片 | 反代改写后本机直连；无反代时请求原 URL | 无 |

## 图片反代解析

| `pixiv_proxy_url` | `pixiv_image_proxy_host` | 图片下载 |
| --- | --- | --- |
| 空 | 空 | 原 Pixiv 图片 URL，本机直连 |
| 已配置 | 空 | 自动改写为 `i.pixiv.re`，本机直连 |
| 任意 | 自定义主机 | 改写为自定义反代，本机直连 |

反代只替换 Pixiv 图片 URL 的 scheme/host，保留 path、query 和 fragment。

```text
https://i.pximg.net/c/.../123_p0_master1200.jpg
https://i.pixiv.re/c/.../123_p0_master1200.jpg
```

Lolicon 与 Pixiv 返回的图片统一经过该规则。非 Pixiv 图片 URL 不改写。

## 代理协议

`pixiv_proxy_url` 支持：

- `http://`
- `https://`
- `socks4://`
- `socks4a://`
- `socks5://`
- `socks5h://`

`socks4a://` 和 `socks5h://` 会规范化为对应 SOCKS 协议，并使用代理端 DNS 解析。

## 失败边界

- Lolicon 失败：尝试 Pixiv 回退。
- Pixiv API 代理失败：不静默改为直连。
- 图片反代失败：本次图片下载失败，不向反代发送 refresh token。
- 公共反代不提供可用性保证；可通过 `pixiv_image_proxy_host` 切换到自建服务。

## 验证脚本

```powershell
python scripts/test_pixiv_proxy.py
```

脚本直接调用 Pixiv API，不调用 Lolicon；Pixiv OAuth/API 使用输入的代理，图片默认通过 `i.pixiv.re` 本机直连下载。
