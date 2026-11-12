# 配置说明

配置真源是根目录 `_conf_schema.json`，已按功能分为 6 个 object 分组，WebUI 配置页会以折叠区块呈现。修改配置字段时必须同步更新 README、本文档和相关测试。

## 1. Pixiv 图源与下载（`pixiv_source`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `pixiv_refresh_token` | `string` | 空 | 使用 Pixiv 作为首选或回退时需要填写；留空直接使用 Lolicon |
| `preferred_image_source` | `enum` | `Lolicon` | 下拉选择 `Lolicon` / `Pixiv`；首选请求失败或无结果时尝试另一源 |
| `lolicon_exclude_ai` | `bool` | `true` | 仅向 Lolicon API 传递 excludeAI=true/false；R18 与普通混合由当前会话强制普通分级决定 |
| `lolicon_image_proxy_origins` | `text` | 空 | 每行一个 http(s) origin，最多 5 个按序尝试；仅改写允许列表内 Pixiv 图片主机 |
| `max_count` | `int` | `5` | 命令、正则触发和模型工具的单次数量上限，范围 1–20；模型工具省略 count 时取 1 张，默认值不覆盖已保存的配置 |
| `p_coin_cost` | `int` | `20` | 命令、正则触发和模型工具共用的每张金币，范围 0–200；0 免费；正常返回与发送超时均扣费，下载／构建失败及其他发送异常不扣费 |
| `image_quality` | `enum` | `original` | `original`/`large`/`medium`；固定所选画质，超限／不可用时换作品 |
| `auto_downgrade_original_mb` | `float` | `10.0` | 图片大小上限（MiB），适用于所选画质，范围 0–25；0 关闭软上限；保留旧键并沿用已有数值 |
| `forward_threshold` | `int` | `1` | 命令及正则触发使用；模型工具仅 send_mode=auto 时使用。下载张数严格大于此值时合并转发（仅 aiocqhttp）；0 始终合并，范围 0–20 |
| `auto_trigger_enabled` | `bool` | `false` | 固定句式（「来份/张图」）正则自动发图，不控制模型工具 |

`preferred_image_source` 保存并重载后生效，普通发图、签到背景与日历背景共用请求顺序。Lolicon 有标签时搜索、无标签时随机；Pixiv 有标签时搜索、无标签时推荐。无 Pixiv token 时直接使用 Lolicon；两源均失败或无结果时按各入口现有失败路径处理。单张候选被过滤、去重或下载失败不直接触发换源；普通发图耗尽本批可用候选后按相同优先级补取，请求失败或空结果时仍可回退另一源；有效缓存及已保存背景继续复用，不因切换优先级强制重选。无效选项记录日志并使用默认 Lolicon 优先。

普通发图先用同批剩余候选补足张数，每次请求最多取 3 批、实际尝试 20 张作品，同一作品 ID 不重复下载。大小等于上限可发送；超限、所选画质地址缺失或下载失败则换图，不尝试其他画质。反代与源地址重试保持同一画质。跳过候选清理文件并释放临时占用，最终按实际发送调用结果结算；不足时发送已找到的图片。签到和日历仍使用独立画质规则，不应用此软上限。

从首次下载开始，下载和候选补取共享 `request_timeout × max(3, 请求张数)` 秒预算；默认超时 30 秒时，1 张为 90 秒、5 张为 150 秒。预算到期会取消进行中的网络操作，发送已下载图片，允许少于请求张数；首次取源、消息发送和资源清理不计入该预算。大小上限在响应头及分块读取时执行。

旧阈值不会自动改为 10 MiB；使用原图且频繁因大小限制换图时，建议手动将上限调整为 10 MiB。

Lolicon 请求地址固定为 `https://api.lolicon.app/setu/v2`。旧 `lolicon_api_url` 从 schema 删除，由 AstrBot 加载时裁剪；简单 dict 中的残留值也不参与请求，旧自定义地址和留空停用不再生效。图片反代 origin 配置仍只代理图片，不代理 API。

OneBot 发图默认使用本地 `file:///` 路径；仅在明确文件读取失败时，将同一条消息回退到 base64 编码重试一次，超时不回退、不重发。base64 是图片编码，与 AstrBot 4.27.3 的 OneBot 默认发图编码相同；回退仍直接调用 OneBot，不额外尝试 AstrBot 框架发送。file 发送正常返回但没有有效 `message_id` 时仍扣费，并保留图片 10 分钟供协议端读取，不重发。旧 `onebot_image_transport`（顶层及 `pixiv_source` 内）由 AstrBot 在加载时按 schema 裁剪；插件不依赖旧键，也不能恢复框架已裁剪的值。发送异常仅记日志，详见 [OneBot 图片发送](../../README.md#onebot-图片发送)。

## 2. 图片筛选与去重（`content_dedupe`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `filter_manga` | `bool` | `true` | 过滤 Pixiv 结果中的 manga；Lolicon 返回按插画处理 |
| `dedupe_days` | `int` | `1` | 最近 0–7 个北京时间自然日；1 仅当天，非滚动 24 小时；同群共享，不同会话、标签和图源隔离；0 关闭并清空使用记录 |
| `group_content_safety_policies` | `template_list` | `[]` | 群聊内容安全策略：强制普通分级、内置安全词开关、独立自定义屏蔽词与独立作品 ID 黑名单，按群 ID 生效；建议在管理中心维护 |
| `private_content_safety_policies` | `template_list` | `[]` | 私聊内容安全策略：字段同群聊策略，按用户 ID 独立生效；建议在管理中心维护 |
| `group_content_safety_policies_migrated` | `bool` | `false` | 会话策略迁移标记（隐藏） |

`dedupe_days` 不控制签到 JPEG、日历或预览缓存的有效期。窗口缩短会清理超期使用记录，扩大不能恢复已删除历史；配置保存后重载生效。查询、预占用及不同入口的复用规则见 [缓存与去重](cache-and-dedupe.md#2-正式图片去重)。

## 3. 万象画卷联动（`checkin_omnidraw`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `checkin_omnidraw_link_enabled` | `bool` | `false` | 联动总开关；开启后才会检测已安装的万象画卷插件，启用商店生图额度、签到发放额度、`/签到状态` 额度展示等全部联动功能 |
| `checkin_omnidraw_quota_cost` | `int` | `75` | 生图额度每张单价，范围 0–300；实际花费 = 单价 × 张数 |
| `checkin_omnidraw_quota_default` | `int` | `1` | 不指定张数时默认购买张数，范围 1–50 |
| `checkin_omnidraw_quota_daily_max` | `int` | `10` | 每人每日购买上限，0 表示不限（最大 30）；签到赠送不受此限 |

仅在 `checkin_omnidraw_link_enabled` 开启、且实际安装万象画卷并启用其每日生图限制时生效。购买张数 `签到商店 生图 <张数>`（1–50），额度当日有效，跨天清零。

## 4. 签到基础（`checkin_basic`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `checkin_enabled` | `bool` | `true` | 签到开关 |
| `checkin_bot_name` | `string` | `neko` | 签到卡片 bot 角色名 |
| `checkin_card_quality_tier` | `enum` | `省流量` | `省流量`(960×540+medium)/`清晰`(1248×702+large)/`极致`(1728×972+large)；日历同档但尺寸分别为 1600×900／2080×1170／2880×1620；普通重复签到保持当天记录档位 |
| `checkin_avatar_enabled` | `bool` | `true` | QQ 平台尝试在卡片显示用户头像 |
| `checkin_greeting_mode` | `enum` | `hitokoto` | `local`(本地事件)/`hitokoto`(一言 API)/`ai`(文本模型) |
| `checkin_hitokoto_categories` | `list` | `["全部"]` | 一言类型多选 |
| `checkin_ai_greeting_provider_id` | `string` | 空 | AI 问候模型，留空用当前会话模型 |
| `checkin_ai_greeting_prompt` | `text` | 见 schema | AI 问候提示词，可自定义角色语气 |
| `checkin_ai_greeting_timeout` | `float` | `8.0` | AI 问候超时，1–30 秒 |
| `checkin_hitokoto_timeout` | `float` | `5.0` | 一言请求超时，1–15 秒 |
| `checkin_background_mode` | `enum` | `pixiv_daily` | `pixiv_daily`(每日自动选背景)/`custom`(固定背景文件) |
| `checkin_background_tag` | `string` | 空 | 多标签随机排列逐个尝试，使用记录按最终标签隔离；留空按所选优先级请求 Lolicon 随机或 Pixiv 推荐作品 |
| `checkin_custom_background` | `string` | 空 | 管理员本地背景路径，推荐 3:4 竖图 |

## 5. 签到商店与定价（`checkin_shop`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `checkin_background_refresh_cost` | `int` | `100` | 签到后刷新背景的金币，范围 0–300；0 免费 |
| `checkin_theme_cost` | `int` | `1500` | 非默认签到主题价格，范围 0–5000；默认「米白」始终免费 |

`checkin/themes.py` 的 `price` 仅作读取失败的兜底。主题编号：`00` 米白(免费)、`01` 浅蓝、`02` 红黑、`03` 黄黑、`04`–`07` 四季系列(新柳/荷风/丹枫/寒梅)。`/签到主题 查看|购买|切换 <编号>` 支持编号、ID、中文名。

## 6. 运行参数（`runtime`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `request_timeout` | `float` | `30.0` | 单张图片下载最大等待，5–120 秒 |
| `rate_limit_seconds` | `int` | `3` | 同一用户两次请求最小间隔，0 禁用，0–60 秒 |
| `webui_font_source` | `enum` | `mirror` | 插件管理中心 Google Fonts 加载方式：`mirror`(国内镜像)/`official`/`none` |

## 会话内容安全策略

群聊和私聊策略均为配置文件中的模板列表，可在插件配置页或管理中心按群 ID / 用户 ID 独立添加、编辑、删除。每条策略包含普通分级、内置安全词、独立自定义屏蔽词和独立作品 ID 黑名单。
独立屏蔽词按保留标点的词条分别保存，例如 `r18g` 与 `r-18g` 是两个词条；全角/半角及大小写等价项会判重，过滤匹配仍忽略常见分隔符。

| 启用内置安全词 | 生效规则 |
| --- | --- |
| 开启 | 内置安全词 + 内容安全页全局自定义屏蔽词/作品黑名单；忽略当前会话的两份独立列表。 |
| 关闭 | 停止以上三类全局约束；仅使用当前会话独立自定义屏蔽词和独立作品 ID 黑名单。 |

独立列表支持分别应用到所有群聊策略、所有私聊策略或全部策略；批量操作只覆盖对应字段，并在一次保存中完成，失败时回滚。缺少会话上下文、策略不存在或读取异常时严格启用普通分级和内置安全词。

## 升级基线

v3.8.7 的配置升级基线为已成功完成迁移的 v3.8.0，配置只保留六个分组。顶层旧扁平兼容键、`content_dedupe.dedupe_ttl_hours`、`content_dedupe.dedupe_days_migrated` 和 `runtime._grouped_config_migrated` 已移除；正式字段的默认值与策略模板不变。`group_content_safety_policies_migrated` 仍用于 SQLite 旧群策略迁移，不能作为无用标记删除。

升级前备份插件配置 JSON 和数据目录。早于 v3.8.0 的版本须先安装 v3.8.0、成功启动，并确认自定义值及群／私聊策略已保存到对应分组后再升级。仅存在于顶层的值不属于支持范围；AstrBot 4.27.3 在插件初始化前删除 schema 外的键，插件无法在裁剪后找回旧值。若来自 v2.8.x 或已有数据但数据库无 schema 版本号，先使用 v3.0.0 完成签到数据迁移，再经过 v3.8.0 完成配置迁移。不要先升级再依赖回退旧版本恢复配置。

## 配置维护规则

- README 配置表必须和 `_conf_schema.json` 保持一致。
- 运行时通过 `main.py` 的 `_cfg_str`/`_cfg_int`/`_cfg_float`/`_cfg_bool` 读取。正式配置由 AstrBot 按分组加载；读取层保留简单 dict 支持，但不承担旧扁平配置迁移。不要在业务流程散落 `self.config.get(...)`。
- 分组后存盘为嵌套 `config[组][键]`；新增配置项时加入对应组的 `items`，读取层无需改动。
- 删除、重命名或改字段类型时必须说明兼容影响。
- `forward_threshold` 按下载张数判断，仅 aiocqhttp 合并转发；旧 `send_as_forward` 仅在新字段缺失时兼容（`true`→0，`false`→20）。
- `dedupe_days` 缩短天数会在重载时清理超期记录，增加天数无法恢复已清理历史。
- `image_quality` 不影响签到背景，签到卡/日历背景画质由 `checkin_card_quality_tier` 独立控制。
- `pixiv_refresh_token` 留空时跳过 Pixiv，仅请求 Lolicon；失败或无结果时不再有可用回退源。
- schema 顶层仅定义六个正式分组，不再复制隐藏兼容键。策略服务只读写 `content_dedupe` 的正式模板；分组中的空列表不得恢复已停用的顶层策略。

## 模型取图参数

v3.8.8 无新增持久配置。原生工具沿用 `max_count`、`p_coin_cost`、`checkin_enabled`、图源、画质、大小上限和现有群聊／私聊安全策略。`tag`、`count`、`send_mode`、`orientation`、`aspect_ratio`、`ratio_tolerance`、`min_width`、`min_height`、`max_size_mib` 仅作用于本次工具调用，完整约束见 [随包 Skill](../../skills/get-px-image/SKILL.md)。默认 `single`；`auto` 才使用 `forward_threshold`，`forward` 仍受 OneBot 平台能力限制。

模型工具数量结果中，`requested_count` 是请求数量，`effective_count` 是应用配置及余额限制后的执行数量，`max_count` 是本次读取的上限，`count_limits` 列出 `max_count`／`balance`（可同时存在）。缩减原因通过 `notice` 说明；实际候选不足另由 `message` 说明，均不代表标签失效。搜索日志分别记录原始请求数、实际执行数与配置上限。

`auto_trigger_enabled` 只控制固定句式正则触发，不控制模型工具注册。模型工具默认逐张直发，仅 `send_mode=auto` 读取合并阈值。比例容差省略时使用 `min(0.1, aspect_ratio/2)`，显式传入仍须非负且小于目标比例，0 表示精确比例。
