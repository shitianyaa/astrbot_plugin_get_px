<div align="center">

# 画境拾珍

一个面向 AstrBot 的 Pixiv 发图插件：搜索插画、查看排行榜、下载作品、每日签到，并在 WebUI 管理图片历史与黑名单。

![AstrBot](https://img.shields.io/badge/AstrBot-plugin-5865f2?style=flat-square)
![Version](https://img.shields.io/badge/version-2.6.1-22c55e?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square)
![Platform](https://img.shields.io/badge/platform-OneBot%20%2F%20aiocqhttp-f97316?style=flat-square)

</div>

## 界面展示

### 签到卡片

![签到卡片预览](docs/images/checkin-card-preview.jpeg)

> 签到卡片支持 Pixiv 每日自动背景或管理员自定义背景；上图为测试预览效果，测试不会写入签到数据。

## 功能一览

| 场景 | 能力 |
| --- | --- |
| 搜图发图 | 按标签搜索 Pixiv 插画，支持数量限制、多页作品、原图自动降级 |
| 排行榜 | 支持日榜、周榜、月榜、男性向、女性向、原创、新人、漫画 8 种榜单 |
| 作品工具 | 查询作品详情，通过作品 ID 下载并发送指定页 |
| 内容过滤 | R18 模式、漫画过滤、标签黑名单、作品 ID 黑名单 |
| 每日签到 | HTML 签到卡片、Pixiv 或自定义背景、金币、好感度、连续签到、加持商店 |
| WebUI 历史 | 查看已发送图片、本地缩略图、Pixiv 链接，并管理作品黑名单 |
| AI 评论 | 可选视觉模型识图，再由文本模型生成一句插画评论 |
| 稳定性 | 请求频率限制、当天去重、发送失败重试、临时文件自动清理 |

> 主要面向 QQ OneBot / aiocqhttp。其他平台会按 AstrBot 能力尽量降级为逐条发送，兼容性请自行测试。

## 快速开始

1. 在 AstrBot WebUI 插件页安装本插件：
   - 下载本仓库 zip 后选择「导入压缩包」
   - 或在插件安装页粘贴仓库地址：`https://github.com/shitianyaa/astrbot_plugin_get_px`
2. 安装完成后进入插件配置，填写 `pixiv_refresh_token`。
3. 如访问 Pixiv 需要代理，填写 `pixiv_proxy_url`，例如 `http://127.0.0.1:7890`。
4. 发送 `/ph` 查看帮助，或直接试试：

```text
/p 初音ミク 3
/pr week 3
/签到
```

## 常用指令

| 指令 | 说明 | 示例 |
| --- | --- | --- |
| `/p [标签] [数量]` | 按标签搜索并发送图片 | `/p 初音ミク 3` |
| `/p [数量]` | 无标签时拉取默认排行榜 | `/p 5` |
| `/pr [类型] [数量]` | 获取指定排行榜 | `/pr week 3` |
| `/prl` | 查看全部排行榜类型 | `/prl` |
| `/pi <作品ID>` | 查看作品详情 | `/pi 12345678` |
| `/pd <作品ID> [页码]` | 下载并发送作品图片 | `/pd 12345678 2` |
| `/签到` | 每日签到并发送签到卡片 | `/签到` |
| `/签到测试` | 管理员预览签到卡片，不写入数据 | `/签到测试` |
| `/签到状态` | 查看累计签到、金币、好感度和加持状态 | `/签到状态` |
| `/签到商店` | 查看好感度加持商品 | `/签到商店` |
| `/购买加持 <天数>` | 购买好感度双倍加持，支持 1/3/7 天 | `/购买加持 3` |
| `/ph` | 查看插件帮助 | `/ph` |

## 自然语言触发

开启 `auto_trigger_enabled` 后，可以不带命令前缀触发搜图。

| 触发语 | 效果 |
| --- | --- |
| `来一份图` | 发送 1 张默认排行榜图片 |
| `来三张初音ミク图` | 搜索「初音ミク」并发送 3 张 |
| `来两张萝莉图` | 搜索「萝莉」并发送 2 张 |
| `来张风景图` | 搜索「风景」并发送 1 张 |
| `签到` | 触发每日签到 |

## WebUI 图片历史

插件会在 AstrBot WebUI 插件页提供「图片历史」页面。

- 记录搜索、排行榜、作品下载和签到 Pixiv 背景中成功发送的图片。
- 保存标题、作者、作品 ID、页码、来源、R18 标记、尺寸、会话、发送时间、文件大小和 Pixiv 链接。
- 只保留本地缩略图和元数据，不保存原始发送图片。
- 默认最多保留最近 200 条记录，超过后自动清理旧记录和对应缩略图。
- 可在页面中把作品加入黑名单；黑名单会影响搜索、排行、签到背景、详情和下载。

## 每日签到

签到数据按用户全局统计。同一用户每天只能成功签到一次，首次签到会获得金币和好感度，连续签到会提高奖励。

| 行为 | 规则 |
| --- | --- |
| 首次签到 | 发放金币和好感度，生成签到卡片 |
| 连续签到 | 连续天数增加，奖励随连续天数提升 |
| 漏签 | 连续天数重置为 1，累计签到天数不清零 |
| 重复签到 | 不重复发奖，只扣好感度：每次 `-0.20`，当天最多扣 `1.00`，最低到 `-10` |
| 签到测试 | 管理员预览卡片，不写入签到数据，也不写入图片历史 |

### 加持商店

| 商品 | 价格 |
| --- | --- |
| 好感度双倍加持 1 天 | 200 金币 |
| 好感度双倍加持 3 天 | 500 金币 |
| 好感度双倍加持 7 天 | 1000 金币 |

加持购买后立即记录有效期。如果当天已经签到，则从明天签到开始影响收益；重复购买会延长有效期。

### 好感度等级

| 好感度 | 等级 |
| --- | --- |
| `< 0` | 排斥 |
| `0 - 9.99` | 陌生 |
| `10 - 29.99` | 熟悉 |
| `30 - 69.99` | 亲近 |
| `70 - 139.99` | 信赖 |
| `140+` | 挚友 |

签到卡片固定渲染为 `960x540` 的 16:9 横图。背景可以使用 Pixiv 每日自动背景，也可以指定本地图片。Pixiv 背景会沿用 R18 模式、拉黑标签和作品 ID 黑名单，并同时参考当天 SQLite 索引和图片历史避开已用作品；当前页候选都用过时会自动切换下一页。正式签到会在下载背景前预占用作品索引，下载、渲染或发送失败时释放占用，减少并发签到拿到同一张背景的概率。自定义背景不可用时会继续尝试 Pixiv 背景；如果 Pixiv 下载、记录或 HTML 渲染失败，签到奖励仍会发放，并回退为纯文字结果。

## 后续计划

- 继续优化签到卡片的整体布局，让头像、签到统计、今日奖励和好感度进度条的层级更清楚。

## 排行榜类型

| 类型 | 含义 | 说明 |
| --- | --- | --- |
| `day` | 今日排行榜 | 每日综合排名 |
| `week` | 本周排行榜 | 默认榜单，作品质量稳定 |
| `month` | 本月排行榜 | 每月综合排名 |
| `day_male` | 男性向日榜 | 男性用户偏好的每日排名 |
| `day_female` | 女性向日榜 | 女性用户偏好的每日排名 |
| `week_original` | 原创周榜 | 原创作品的每周排名 |
| `week_rookie` | 新人周榜 | 新注册作者的每周排名 |
| `day_manga` | 漫画日榜 | 漫画或多页作品的每日排名 |

## 推荐配置

| 配置 | 建议 |
| --- | --- |
| `pixiv_refresh_token` | 必填，没有 token 插件无法请求 Pixiv |
| `pixiv_proxy_url` | 访问 Pixiv 不稳定时填写代理 |
| `pixiv_r18` | 群聊建议保持 `0`，即仅非 R18 |
| `image_quality` | 想省流量用 `large`，想优先原图用 `original` |
| `send_as_forward` | QQ 场景建议开启，多图会以合并转发发送 |
| `dedupe_ttl_hours` | 保持默认即可，同群同标签当天尽量不重复 |
| `auto_trigger_enabled` | 想让「来张图」生效时再开启 |
| `ai_enabled` | AstrBot 已配置视觉模型和文本模型后再开启 |

<details>
<summary>完整配置项</summary>

| 配置 | 说明 | 默认值 |
| --- | --- | --- |
| `pixiv_refresh_token` | Pixiv refresh_token，必填 | 空 |
| `pixiv_proxy_url` | 代理地址，支持 `http://`、`socks5://` | 空 |
| `pixiv_r18` | R18 模式：`0` 仅非 R18，`1` 仅 R18，`2` 混合 | `0` |
| `filter_manga` | 过滤漫画作品；主动请求 `day_manga` 时保留后门 | `true` |
| `blacklist_tags` | 拉黑标签，多个标签可用逗号、顿号、分号或换行分隔；留空会回退默认拉黑标签 | `furry,裸体,全裸,触手,露出,nsfw` |
| `pixiv_ranking_mode` | 无标签时使用的默认排行榜类型 | `week` |
| `max_count` | 单次最大发送数量，范围 1-20 | `5` |
| `dedupe_ttl_hours` | 普通发图当天去重；设为 `0` 关闭普通发图去重；当前按自然日去重，不按小时滚动过期 | `24` |
| `request_timeout` | 单张图片下载超时，单位秒 | `30` |
| `image_quality` | 图片质量：`original`、`large`、`medium` | `original` |
| `auto_downgrade_original_mb` | 原图超过该大小时自动降级，单位 MiB；`0` 为禁用 | `3.0` |
| `send_as_forward` | 多图以合并转发发送；非 QQ 平台不支持时自动逐条发送 | `true` |
| `auto_trigger_enabled` | 自然语言自动触发 | `false` |
| `checkin_enabled` | 签到开关 | `true` |
| `checkin_bot_name` | 签到卡片中的 bot 角色名 | `neko` |
| `checkin_background_mode` | 签到背景模式：`pixiv_daily` 或 `custom`；自定义背景不可用时会继续尝试 Pixiv 背景 | `pixiv_daily` |
| `checkin_background_tag` | 签到 Pixiv 背景标签，多个标签可用逗号、顿号、分号或换行分隔，每次签到随机确定尝试顺序 | 空 |
| `checkin_background_aspect_ratio` | 签到 Pixiv 背景优先比例，如 `16:9`、`1:1`、`2.2:1` | `16:9` |
| `checkin_background_aspect_tolerance` | 比例容差，`0.25` 表示允许上下 25% | `0.25` |
| `checkin_custom_background` | 本地固定背景路径，推荐 16:9 图片 | 空 |
| `checkin_avatar_enabled` | 签到卡片显示用户头像 | `true` |
| `rate_limit_seconds` | 同一用户请求频率限制，单位秒；`0` 为禁用 | `3` |
| `ai_enabled` | AI 识图评论开关 | `false` |
| `ai_probability` | AI 识图触发概率，范围 0-100 | `30` |
| `ai_max_images` | 每次最多分析的图片数量 | `3` |
| `ai_pre_message` | AI 识图前发送的提示消息 | `让我先品鉴一番，你稍等喵~` |
| `ai_vision_provider_id` | 视觉模型 ID，留空时自动选择 | 空 |
| `ai_comment_provider_id` | 评论模型 ID，留空时使用当前会话模型 | 空 |
| `ai_vision_prompt` | 发送给视觉模型的提示词 | 见配置页 |
| `ai_comment_prompt` | 评论模型提示词，使用 `{description}` 注入识图结果 | 见配置页 |
| `webui_font_source` | WebUI 字体来源：`mirror`、`official`、`none` | `mirror` |

</details>

## 数据与清理

- 当天去重使用 SQLite 记录，同一群聊或私聊内，同一标签或排行榜当天尽量不重复。
- 签到 Pixiv 背景会同时参考当天 SQLite 索引和图片历史；正式签到会预占用索引，失败时释放占用，当前页候选全用过时自动翻页。
- 图片历史和黑名单保存在 AstrBot 插件数据目录中。
- 发送用原图或大图是临时文件，发送完成后会自动清理。
- 图片历史删除或加入黑名单不会清空当天已发作品索引。

## 获取 Pixiv Token

可以使用 [pixiv-token](https://github.com/shitianyaa/pixiv-token) 获取 Pixiv `refresh_token`，然后填入插件配置的 `pixiv_refresh_token`。

## 依赖

```text
pixivpy-async
aiohttp
Pillow
```

## 致谢

- Pixiv 图片获取基于 [pixivpy-async](https://github.com/Mikubill/pixivpy-async)
- 历史缩略图生成基于 [Pillow](https://python-pillow.org/)
- 每日签到设计参考 [zhenxun_bot](https://github.com/zhenxun-org/zhenxun_bot)
