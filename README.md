# Pixiv 发图

AstrBot 插件 — 通过标签搜索 Pixiv 插画并发送图片。

## 功能

- 🔍 按标签搜索 Pixiv 插画并发送图片
- 📊 支持 8 种排行榜（今日/本周/本月/男性向/女性向/原创/新人/漫画）
- 🔎 作品详情查看（ID/标题/作者/标签/浏览量/收藏数）
- 🔒 R18 过滤（仅非R18 / 仅R18 / 混合）
- ⏱️ 请求频率限制（防刷屏）
- 🌐 支持 HTTP/SOCKS5 代理
- 📄 多页作品支持
- 🤖 AI 识图评论（概率触发，支持自定义提示词）
- 📱 多平台适配（QQ/微信等，非QQ平台自动降级为逐条发送，兼容性请自行测试）

## 指令

### 搜索指令

| 指令 | 说明 | 示例 |
|------|------|------|
| `/p [标签] [数量]` | 按标签搜索并发送图片 | `/p 初音ミク 3` |
| `/p [数量]` | 无标签时拉取排行榜 | `/p 5` |

### 管理指令

| 指令 | 说明 | 示例 |
|------|------|------|
| `/pr [类型] [数量]` | 获取指定排行榜 | `/pr day 3` |
| `/prl` | 查看所有排行榜类型 | |
| `/pi <作品ID>` | 查看作品详情 | `/pi 12345678` |
| `/ph` | 查看帮助 | |

## 排行榜类型

| 类型 | 含义 | 说明 |
|------|------|------|
| `day` | 今日排行榜 | 每日综合排名 |
| `week` | 本周排行榜（默认） | 每周综合排名，作品质量最高 |
| `month` | 本月排行榜 | 每月综合排名 |
| `day_male` | 男性向日榜 | 男性用户偏好的每日排名 |
| `day_female` | 女性向日榜 | 女性用户偏好的每日排名 |
| `week_original` | 原创周榜 | 无标签/原创作品的每周排名 |
| `week_rookie` | 新人周榜 | 新注册作者的每周排名，发现宝藏的好途径 |
| `day_manga` | 漫画日榜 | 漫画/多页作品的每日排名 |

## 配置项

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `pixiv_refresh_token` | Pixiv refresh_token（必填） | - |
| `pixiv_proxy_url` | 代理地址 | 空（直连） |
| `pixiv_r18` | R18 模式：0=仅非R18，1=仅R18，2=混合 | 0 |
| `pixiv_ranking_mode` | 默认排行榜类型（无标签时使用） | week |
| `max_count` | 单次最大发送数量（1-20） | 5 |
| `request_timeout` | 单张图片下载超时（秒） | 30 |
| `image_quality` | 图片质量：original / large / medium | original |
| `send_as_forward` | 合并转发发送（聊天记录形式，规避审查） | 开启 |
| `rate_limit_seconds` | 同一用户请求频率限制（秒） | 3 |
| `ai_enabled` | AI 识图开关 | 关闭 |
| `ai_probability` | AI 识图触发概率（0-100%） | 30 |
| `ai_max_images` | AI 识图最大数量 | 3 |
| `ai_pre_message` | AI 识图预回复消息 | 让我先品鉴一番，你稍等喵~ |
| `ai_vision_provider_id` | 识图模型（下拉选择，留空自动） | 空 |
| `ai_comment_provider_id` | 评论模型（下拉选择，留空自动） | 空 |
| `ai_vision_prompt` | 识图提示词 | 描述画风、构图、角色等 |
| `ai_comment_prompt` | 评论提示词（{description}=识图结果） | 根据描述写评论（50字） |

## 获取 Token

使用 [pixiv-token](https://github.com/shitianyaa/pixiv-token) 获取 Pixiv refresh_token。

## 安装

以下方式任选其一：

1. **下载压缩包**：下载本项目为 zip，通过 AstrBot WebUI 插件页的「导入压缩包」安装
2. **粘贴链接**：在 AstrBot WebUI 插件页点击「安装」，粘贴本项目 GitHub 链接

## 致谢

- Pixiv 图片获取基于 [pixivpy-async](https://github.com/Mikubill/pixivpy-async)
