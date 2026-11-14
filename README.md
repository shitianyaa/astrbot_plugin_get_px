<div align="center">

# 画境拾珍

<img src="https://count.getloli.com/@astrbot-plugin-get-px?name=astrbot-plugin-get-px&theme=booru-jaypee&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto" alt="count" />

AstrBot 图片与签到插件，支持 Lolicon / Pixiv 搜图、模型取图、每日签到和金币商店。

![AstrBot](https://img.shields.io/badge/AstrBot-plugin-5865f2?style=flat-square)
![Version](https://img.shields.io/badge/version-3.8.8-22c55e?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)
![Platform](https://img.shields.io/badge/platform-OneBot%20%2F%20aiocqhttp-f97316?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-3b82f6?style=flat-square)

<a href="https://qm.qq.com/q/cPQnFNtdN6"><img alt="QQ Group" src="https://img.shields.io/badge/QQ%E7%BE%A4-Bot%E6%B5%8B%E8%AF%95%E7%BE%A4-12B7F5?style=flat-square&logo=tencentqq&logoColor=white" /></a>

> 欢迎来 QQ 群 **209949919** 交流、讨论，一起玩耍！

<br>
<img src="logo.png" alt="画境拾珍 Logo" width="180">

</div>

## 目录

- [快速开始（安装、升级、Pixiv Token 获取）](#快速开始)
- [图片获取（指令、模型取图、收费）](#图片获取)
- [签到与商店（每日签到、主题、金币、万象画卷联动）](#签到与商店)
- [管理与配置（WebUI、常用配置、内容安全）](#管理与配置)
- [常见问题（图片画质、下载失败、反代设置）](#常见问题)
- [更多文档（完整指令、配置说明、更新日志）](#更多文档)

## 快速开始

### 安装与首次使用

1. 在 AstrBot WebUI 插件页安装，粘贴仓库地址：`https://github.com/shitianyaa/astrbot_plugin_get_px`；也可下载仓库 zip 后选择「导入压缩包」。
2. 默认使用 Lolicon，无需 Token。需要 Pixiv 搜索、榜单或作者作品时，填写 `pixiv_refresh_token`（[获取方法](#获取-pixiv-token)）。
3. 向 Bot 发送：

```text
/p 初音ミク 3
/签到
/签到状态
/签到日历 2026-08
/刷新背景
/签到帮助
```

主要支持 QQ OneBot / aiocqhttp，其他平台需自行测试兼容性。

签到卡和日历依赖 AstrBot T2I 渲染服务。公共服务不稳定时，建议[自行部署 T2I](https://docs.astrbot.app/others/self-host-t2i.html)。

全部指令见[指令参考](docs/user/commands.md)。文档使用默认前缀 `/`，可替换为 AstrBot 配置的其他唤醒前缀；指令冲突可在 AstrBot 指令管理中调整。

### 获取 Pixiv Token

使用 Chrome / Edge 浏览器扩展 [pixiv-token-extension](https://github.com/shitianyaa/pixiv-token-extension)：

1. 按扩展仓库说明安装；从源码加载时选择其中的 `extension` 文件夹。
2. 在浏览器登录 Pixiv，点击扩展图标，等待获取 Token。
3. 复制 `refresh_token`，填入本插件配置的 `pixiv_refresh_token`，保存并重载。

### 升级提醒

> [!WARNING]
> 升级前备份插件配置和数据目录。早于 v3.8.0 的版本须先升级到 v3.8.0 并确认配置迁移成功；v2.8.x 或已有数据库无版本号时，还须先经过 v3.0.0 完成数据迁移。详见[升级说明](docs/project/configuration.md#升级基线)。从已完成迁移的 v3.8.0 及之后版本升级到 v3.8.8，无需新增配置。

## 图片获取

### 指令取图

| 指令 | 说明 |
| --- | --- |
| `/p 初音ミク 3` | 按标签取 3 张图片 |
| `/p 5` | 随机取 5 张图片 |

### 模型原生取图

和模型聊天时，可以这样说：

- 「来三张初音ミク的竖图」
- 「看看 Pixiv 最新周榜」
- 「把这个作品链接的第 2 张发给我」
- 「查一下这个作者有哪些作品」

插件提供取图、作品查询、作者查询和余额查询四个原生工具。搜索、推荐、榜单、作者、关联作品和指定作品模式需要 Pixiv Token；默认模式沿用配置的图源优先级。

公开查询免费，可按需查看点赞、收藏和浏览量。用户要热门图时，模型可先比较本批候选的热度，再选定作品发送；“看看日榜／周榜”只查询，“发几张周榜图”才发图。未明确要求时禁止主动识图，不凭统计数据评价画面质量。

取图沿用数量上限、余额、画质、去重和安全规则，支持方向、比例、像素和文件大小筛选。默认逐张发送，可明确要求 OneBot 合并转发；正常取图不发账单，数量缩减或余额不足时会提醒。追加取图请另发消息。

随包 [get-px-image Skill](skills/get-px-image/SKILL.md) 包含完整调用说明；需要 AstrBot 支持插件 Skill 自动发现。旧版无法读取 Skill 时，仍可使用原生工具及其参数描述。消息需要进入模型对话且工具可用，实际是否调用取决于模型；不使用模型时可直接用 `/p`。

### 收费说明

普通发图默认每张 **20 金币**，可通过 `p_coin_cost` 调整，设为 `0` 免费。发送调用正常返回或超时均扣费；超时不重发，即使最终未出图也扣费。下载、构建失败及其他发送异常不扣费。

## 签到与商店

### 每日签到

| 指令 | 说明 |
| --- | --- |
| `/签到` | 每日签到，领取金币和好感度 |
| `/签到状态` | 查看金币、好感度和连签 |
| `/签到日历 [YYYY-MM]` | 查看个人月度签到日历 |
| `/签到排行 今日\|月榜\|连签\|累计` | 查看当前群排行 |
| `/签到商店 查看` | 查看加持、背景刷新和生图额度 |
| `/签到主题 查看 <编号>` | 免费预览主题 |
| `/刷新背景` | 付费更换当天签到背景 |

也可直接发送纯文本「签到」。每天领取一次奖励，在不同群签到会分别记录群榜，不重复发金币。重复签到复用当天卡片；安全规则不同的聊天分别缓存。

金币可用于加持、刷新背景和购买主题。背景刷新默认 **100 金币**，非默认主题每套 **1500 金币**，均可配置；默认「米白」免费。生日、称号、问候等用法见[签到说明](docs/user/checkin.md)。

### 签到卡主题

| `04` · 新柳 | `05` · 荷风 |
| :---: | :---: |
| ![新柳](templates/checkin_themes/spring/preview.png) | ![荷风](templates/checkin_themes/summer/preview.png) |
| `06` · 丹枫 | `07` · 寒梅 |
| ![丹枫](templates/checkin_themes/autumn/preview.png) | ![寒梅](templates/checkin_themes/winter/preview.png) |

签到卡支持 `省流量`（960×540）、`清晰`（1248×702）和 `极致`（1728×972）三档。`/签到主题 查看 <编号>` 可免费看预览（如 `/签到主题 查看 1`），不扣金币、不切换主题。

### 万象画卷联动

同时安装[万象画卷](https://github.com/diaomin66/astrbot_plugin_omnidraw/)并启用其每日生图限制后，可开启 `checkin_omnidraw_link_enabled`：

- 签到领取生图额度，默认随机 1–3 张，实际按万象画卷配置发放；两边签到不会重复领取。
- 用 `签到商店 生图 [张数]` 购买额度，发放失败退回金币。
- 在 `/签到状态` 查看额度；额度当日有效，跨天清零。

联动默认关闭，对方插件不可用或版本不兼容时，不影响签到。价格和每日购买上限见[联动配置](docs/project/configuration.md#3-万象画卷联动checkin_omnidraw)。

## 管理与配置

### WebUI 插件管理中心

<div align="center">
<img src="Webui.png" alt="插件管理中心界面" width="100%">
</div>

<br>

在 AstrBot 插件页打开「pluginCenter」，可以查看群排行和趋势、调整成员金币与好感度、管理安全词和黑名单，以及导入／导出签到备份。

修改成员数值只影响当前资料，不回写历史奖励、群排行或已生成卡片。

### 常用配置

| 配置 | 用法 |
| --- | --- |
| `preferred_image_source` | 默认 `Lolicon`，可改为 `Pixiv` 优先；首选请求失败或无结果时尝试另一源，无 Token 时只用 Lolicon |
| `pixiv_refresh_token` | 使用 Pixiv 时填写 |
| `max_count` | 单次最多取图张数，默认 `5` |
| `p_coin_cost` | 每张图片金币，默认 `20`，`0` 免费 |
| `image_quality` | 默认 `original` 原图，也可选 `large` / `medium` |
| `auto_downgrade_original_mb` | 图片大小上限，默认 `10` MiB；超限换作品，`0` 关闭软上限 |
| `forward_threshold` | 仅 aiocqhttp：`0` 始终合并，默认 `1` 表示超过 1 张时合并；模型工具仅自动发送模式使用此值 |
| `checkin_card_quality_tier` | 签到卡和日历画质：省流量／清晰／极致，默认省流量 |
| `dedupe_days` | 默认只去重北京时间当天，可设 `0–7`；`0` 关闭，不同会话、标签和图源独立 |
| `lolicon_image_proxy_origins` | Lolicon 图片地址无法访问时填写，每行一个 origin |

修改后保存并重载。完整配置见[配置说明](docs/project/configuration.md)。

### 内容安全

在管理中心按群 ID 或私聊用户 ID 设置普通分级、安全词和作品黑名单：

- 开启「内置安全词」：使用内置词及全局自定义词／黑名单，忽略当前会话独立列表。
- 关闭「内置安全词」：只使用当前会话独立词／黑名单，以上全局约束也会停用。
- 缺少会话策略或读取失败时，默认启用普通分级和内置安全词。

详见[会话内容安全策略](docs/project/configuration.md#会话内容安全策略)。

## 常见问题

### 为什么设置了原图，发出来仍然模糊？

插件按 `image_quality` 下载，不压缩、不自动降档。设为 `original` 后，超限或下载失败会换作品。检查原作品分辨率，以及客户端是否显示了预览图。

图片大小上限默认 **10 MiB**，旧配置的 `3.0` 不会自动更新。频繁换图时可调大 `auto_downgrade_original_mb`，或设为 `0` 关闭软上限；单张仍有 **50 MiB** 硬上限。签到卡和日历请调整独立的 `checkin_card_quality_tier`。

### Lolicon 图片下载失败，反代怎么填？

在 `lolicon_image_proxy_origins` 中一行填写一个完整 origin，如 `https://i.pixiv.re`，最多 5 个；不填路径或作品 ID。保存后重载，公共服务可能限速或停服，填写方法见[Lolicon 图片反代](docs/project/configuration.md#lolicon-图片反代)。

反代只改写 Lolicon 图片地址，不代理 API 或 Pixiv 登录。需要 HTTP/SOCKS 出站代理时，使用 [`proxy` 分支](https://github.com/shitianyaa/astrbot_plugin_get_px/tree/proxy)。

## 更多文档

| 文档 | 内容 |
| --- | --- |
| [指令参考](docs/user/commands.md) | 全部指令与纯文本签到 |
| [签到说明](docs/user/checkin.md) | 奖励、商店、主题、生日、称号与问候 |
| [配置说明](docs/project/configuration.md) | 完整配置、图源、安全策略与升级说明 |
| [模型工具 Skill](skills/get-px-image/SKILL.md) | 工具参数、查询与发送规则 |
| [缓存与去重](docs/project/cache-and-dedupe.md) | 去重范围、缓存与清理 |
| [项目架构](docs/project/architecture.md) | 模块划分与发送流程 |
| [T2I 渲染说明](docs/dev/t2i-rendering.md) | 自建服务与渲染排查 |
| [更新日志](CHANGELOG.md#v388-2026-11-11) | 各版本变化 |

### 依赖

```text
pixivpy-async
aiohttp
Pillow
lunar-python
```

### 致谢

- 默认图片源由 [Lolicon API](https://api.lolicon.app/) 提供
- Pixiv 图片源基于 [pixivpy-async](https://github.com/Mikubill/pixivpy-async)
- 作品黑名单缩略图生成基于 [Pillow](https://python-pillow.org/)
- 签到每日一言由 [Hitokoto API](https://github.com/hitokoto-osc/hitokoto-api) 提供，感谢一言开源社区和公共 API 服务
- 签到卡片内置字体由 [霞鹜文楷轻便版](https://github.com/lxgw/LxgwWenKai-Lite) 生成，采用 SIL Open Font License 1.1 授权
- 每日签到设计参考 [zhenxun_bot](https://github.com/zhenxun-org/zhenxun_bot)
- 万象画卷联动接入 [astrbot_plugin_omnidraw](https://github.com/diaomin66/astrbot_plugin_omnidraw/)（作者 雪碧bir），生图额度由对方插件管理
- 跨插件联动桥接模式参考 [astrbot_plugin_private_companion](https://github.com/menglimi/astrbot_plugin_private_companion)（作者 menglimi），感谢其私有成员探测与降级策略的设计启发
- [PeeGayhub Telegram 表情包系列](https://t.me/addstickers/PeeGayhub)：插件图标借鉴了该系列表情包风格；图标素材由 GPT 生成。
