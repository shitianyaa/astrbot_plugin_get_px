# T2I 渲染与本地重渲

本文记录插件的 T2I（HTML 转图片）渲染链路、各资产的画布约定，以及网络受限时用自建端点本地重渲的通用方法。帮助图、签到日历、签到卡都走同一链路。

## 运行时链路

```text
Star.html_render(tmpl, data, return_url=False, options)
  → astrbot.core.utils.t2i.NetworkRenderStrategy.render_custom_template
    → 注入 Shiki 运行时（代码高亮）→ POST {endpoint}/text2img/generate
    → return_url=False 时下载为临时文件路径（str，用完 cleanup）
```

- 端点默认取官方列表，可在 AstrBot 配置中自定义。
- 渲染发生在远端浏览器中，`options` 即 Playwright 截图选项（`full_page`、`clip`、`viewport`、`type`、`quality`、`animations`、`device_scale_factor_level`）。

## 画布约定

| 资产 | 模板 | 画布 | 渲染选项要点 |
| --- | --- | --- | --- |
| 帮助图 | `templates/checkin_help/` | 宽 1440 固定，**高随内容自适应** | `full_page` + png，**不传 clip** |
| 签到日历 | `templates/checkin_calendar/` | 设计画布 1600×900，输出随档位 1600/2080/2880 | `full_page` + `clip` + 档位缩放，运行时 jpeg q95 |
| 签到卡 | `templates/checkin_themes/` | 固定尺寸 + 档位缩放 | `clip` + jpeg + `device_scale_factor_level` |

约定：内容型海报（帮助图）高度自适应，新增指令图自动变高，不会被裁切；固定版式（日历、卡片）用 `clip` 钉死尺寸，尺寸约定由测试断言守护（`tests/test_checkin_help_asset.py`、`test_checkin_calendar.py`）。

签到卡与日历的背景图会以 base64 内联进模板，过大时请求体可达 10MB 以上，远端网关直接返回 504 HTML，渲染结果不是 JPEG，三个档位会连续降档失败并最终退回文字。为避免该问题，超过边长或 1.5MB 字节数的背景图会先等比缩放再重编码内联：

- 卡片背景上限 1920px，日历背景上限 2880px（对齐日历极致档输出 2880×1620），各自的调用方传入 `max_edge`，避免先缩小再放大。
- 缩放前对 JPEG 使用 `draft()` 降低解码分辨率；解码前按 40MP 像素预算拦截超大图。透明色图先转换为带 alpha 的像素再缩放，避免重采样混色后丢失透明度；缩放后应用 EXIF 方向。
- 带透明通道的图（RGBA/LA/PA，以及 P/RGB/灰度带 `transparency` 信息）输出 PNG 并保留 alpha；其余输出 JPEG（质量 88）。重编码时应用嵌入的 ICC 配置转换到 sRGB，透明通道不参与色彩转换；无效配置或转换失败沿用空背景回退并记录处理失败日志。
- 重编码输出也受 1,500,000 字节预算约束，超出时继续等比缩小并重新编码，直至满足预算；因此高细节图片可能小于上述边长。该值是沿用的背景预算，base64 部分最多约 2MB，不是 T2I 网关上限，也不包含字体、模板与 AstrBot 注入脚本，不能保证端点不再超时。
- 小图（未超输入阈值）仍保持原始字节直传，不修改源文件。缩小与编码预算日志包含 `encoded_bytes`、`byte_budget`、`target_width`、`target_height`。

所有模板必须自包含：CSS 内联、字体 base64 内联，严禁外部网络 URL。

## 头像与单次渲染资源

QQ 头像复用异步图片下载器及 `request_timeout` 配置，校验后以 data URL 内联，避免远端浏览器访问头像外链。失败时记录错误类型并使用昵称首字占位；下载文件在处理完成、失败或取消时清理。每次卡片降级流程延迟到实际渲染时准备资源，三档复用同一份头像与背景；缓存命中不下载头像。缓存键保留稳定源标识，并以 `resource_version` 区分处理规则，不包含临时路径或下载内容。

画质探针必须显式传入 `--output-dir`，例如 Windows 使用 `python scripts/probe_checkin_t2i_quality.py --output-dir D:/tmp/codex/t2i-quality-probe`，避免在插件目录创建运行态 `data/`。

## 本地重渲帮助图

```bash
# 插件仓库根目录；走 AstrBot 默认 t2i 端点
python -m templates.checkin_help.render_help
```

脚本位置：`templates/checkin_help/render_help.py`（dev-only，复用 `_help_template()` 组装自包含 HTML）。

## 通用方法：自建端点 + 本地代理

默认官方端点不可达或需要测试自建服务时，直接实例化 `NetworkRenderStrategy`，与运行时保持完全一致的注入和下载链路：

```python
import asyncio, os
from pathlib import Path

from astrbot.core.utils.t2i.network_strategy import NetworkRenderStrategy

# aiohttp trust_env 只认 http 代理方案；混合端口也写 http://，不要写 socks5h://
os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:<本地代理端口>")
os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:<本地代理端口>")

renderer = NetworkRenderStrategy(base_url="https://<你的-t2i-服务>")  # 自动补 /text2img

async def main():
    path = await renderer.render_custom_template(
        html,          # 自包含模板字符串（CSS/字体已内联）
        {},            # jinja2 数据
        return_url=False,
        options={"full_page": True, "type": "png",
                 "viewport": {"width": 1440, "height": 1200},
                 "animations": "disabled"},
    )
    Path("out.png").write_bytes(Path(str(path)).read_bytes())

asyncio.run(main())
```

注意事项：

- `base_url` 末尾会自动补 `/text2img`，不要自带该后缀。
- hf.space 等平台有冷启动，首次请求可能要等 30–60 秒，超时给足。
- 端点地址与代理端口是本机环境信息：仓库文档只写占位，具体值放在 gitignore 的本地脚本里（本仓库为 `scratch/render_t2i_assets.py`）。

## 日历出图（同一方法）

日历需要先组装视图数据，再连同模板一起交给渲染器；选项与运行时一致（clip 钉住 1600×900）：

```python
from astrbot_plugin_get_px.checkin.calendar import (
    CHECKIN_CALENDAR_HEIGHT, CHECKIN_CALENDAR_WIDTH,
    build_checkin_calendar_data, get_checkin_calendar_template,
)
from astrbot_plugin_get_px.checkin.quality import CHECKIN_JPEG_QUALITY

data = build_checkin_calendar_data(
    month="2026-08", today_key="2026-08-31",
    records=[...],            # CheckinRecord 夹具，参考下述 _dev 脚本
    events=[...],             # {"start","end","name","is_off_day"}
)
options = {
    "full_page": True, "type": "jpeg",
    "quality": CHECKIN_JPEG_QUALITY,
    "clip": {"x": 0, "y": 0, "width": CHECKIN_CALENDAR_WIDTH, "height": CHECKIN_CALENDAR_HEIGHT},
    "viewport": {"width": CHECKIN_CALENDAR_WIDTH, "height": CHECKIN_CALENDAR_HEIGHT},
    "animations": "disabled",
}
```

现成入口：

```bash
# 走真实 t2i 链路（本机脚本，gitignored，含端点/代理默认值）
# 事件条用产线 collect_calendar_events 收集；today 默认目标月 5 日，
# 保证"当前月只显示未开始"规则下事件胶囊可见，可用 --today 改
python scratch/render_t2i_assets.py calendar [--month YYYY-MM] [--today YYYY-MM-DD]
# 本地 playwright 直渲（无需网络，适合快速看版式；与线上链路略有差异）
python templates/checkin_calendar/_dev/render_production_preview.py
```

数据夹具参考 `templates/checkin_calendar/_dev/render_production_preview.py`；`_dev/` 与 `scratch/` 均已 gitignore，只放预览产物，不放正式资产。

## 相关脚本

- `templates/checkin_help/render_help.py`：重渲帮助图资产（默认端点）。
- `scripts/probe_checkin_t2i_quality.py`：签到卡各画质档位批量渲探针与清晰度报告（默认端点）。
- `scratch/render_t2i_assets.py`：本机自建端点渲染 help / calendar（gitignored）。
