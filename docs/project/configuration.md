# 配置说明

配置真源是根目录 `_conf_schema.json`，已按功能分为 6 个 object 分组，WebUI 配置页会以折叠区块呈现。修改配置字段时必须同步更新 README、本文档和相关测试。

## 1. Pixiv 图源与下载（`pixiv_source`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `pixiv_refresh_token` | `string` | 空 | Lolicon 主源失败时用于 Pixiv 搜索或推荐作品回退；留空不影响 Lolicon 发图 |
| `lolicon_api_url` | `string` | `https://api.lolicon.app/setu/v2` | 首选图片源地址；留空停用 Lolicon，仅 Pixiv 回退 |
| `lolicon_exclude_ai` | `bool` | `true` | 仅向 Lolicon API 传递 excludeAI=true/false；R18 与普通混合由当前会话强制普通分级决定 |
| `lolicon_image_proxy_origins` | `text` | 空 | 每行一个 http(s) origin，最多 5 个按序尝试；仅改写允许列表内 Pixiv 图片主机 |
| `max_count` | `int` | `5` | 单次指令最多发送张数，范围 1–20 |
| `p_coin_cost` | `int` | `20` | `/p` 成功发图每张金币，范围 0–200；0 免费 |
| `image_quality` | `enum` | `original` | `original`/`large`/`medium`；超阈值自动降级 |
| `auto_downgrade_original_mb` | `float` | `3.0` | 原图超过此 MiB 时降级，范围 0–25；0 禁用降级 |
| `forward_threshold` | `int` | `1` | 下载张数严格大于此值时合并转发（仅 aiocqhttp）；0 始终合并，范围 0–20 |
| `auto_trigger_enabled` | `bool` | `false` | 群内自然语言（「来份/张图」）自动触发发图 |

## 2. 图片筛选与去重（`content_dedupe`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `filter_manga` | `bool` | `true` | 过滤 Pixiv 回退结果中的 manga；Lolicon 返回按插画处理 |
| `dedupe_days` | `int` | `1` | 按北京时间自然日去重，0–7；0 关闭并清空记录。同群共享，缩短天数会在重载时清理超期记录 |
| `dedupe_ttl_hours` | `float` | `24.0` | 旧版去重配置（迁移用，隐藏） |
| `dedupe_days_migrated` | `bool` | `false` | 去重配置迁移标记（隐藏） |
| `group_content_safety_policies` | `template_list` | `[]` | 群聊内容安全策略：强制普通分级、内置安全词开关、独立自定义屏蔽词与独立作品 ID 黑名单，按群 ID 生效；建议在管理中心维护 |
| `private_content_safety_policies` | `template_list` | `[]` | 私聊内容安全策略：字段同群聊策略，按用户 ID 独立生效；建议在管理中心维护 |
| `group_content_safety_policies_migrated` | `bool` | `false` | 会话策略迁移标记（隐藏） |

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
| `checkin_card_quality_tier` | `enum` | `省流量` | `省流量`(960×540+medium)/`清晰`(1248×702+large)/`极致`(1728×972+large)；签到日历同档 |
| `checkin_avatar_enabled` | `bool` | `true` | QQ 平台尝试在卡片显示用户头像 |
| `checkin_greeting_mode` | `enum` | `hitokoto` | `local`(本地事件)/`hitokoto`(一言 API)/`ai`(文本模型) |
| `checkin_hitokoto_categories` | `list` | `["全部"]` | 一言类型多选 |
| `checkin_ai_greeting_provider_id` | `string` | 空 | AI 问候模型，留空用当前会话模型 |
| `checkin_ai_greeting_prompt` | `text` | 见 schema | AI 问候提示词，可自定义角色语气 |
| `checkin_ai_greeting_timeout` | `float` | `8.0` | AI 问候超时，1–30 秒 |
| `checkin_hitokoto_timeout` | `float` | `5.0` | 一言请求超时，1–15 秒 |
| `checkin_background_mode` | `enum` | `pixiv_daily` | `pixiv_daily`(每日自动选背景)/`custom`(固定背景文件) |
| `checkin_background_tag` | `string` | 空 | 背景搜索标签，多标签分隔；留空 Lolicon 随机取图 |
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

## 配置维护规则

- README 配置表必须和 `_conf_schema.json` 保持一致。
- 运行时通过 `main.py` 的 `_cfg_str`/`_cfg_int`/`_cfg_float`/`_cfg_bool` 读取，这些方法先遍历分组取值、找不到再回退扁平 key（兼容旧扁平配置与测试）。不要在业务流程散落 `self.config.get(...)`。
- 分组后存盘为嵌套 `config[组][键]`；新增配置项时加入对应组的 `items`，读取层无需改动。
- 删除、重命名或改字段类型时必须说明兼容影响。
- `forward_threshold` 按下载张数判断，仅 aiocqhttp 合并转发；旧 `send_as_forward` 仅在新字段缺失时兼容（`true`→0，`false`→20）。
- `dedupe_days` 缩短天数会在重载时清理超期记录，增加天数无法恢复已清理历史。
- `image_quality` 不影响签到背景，签到卡/日历背景画质由 `checkin_card_quality_tier` 独立控制。
- `pixiv_refresh_token` 留空时 Lolicon 失败直接报错，不进行 Pixiv 回退。
- schema 顶层保留兼容迁移用的 `invisible` 旧扁平键。AstrBot 4.27+ 在加载插件配置时会删除 schema 之外的键，这些 invisible 键让旧扁平值在框架裁剪前存活，`_migrate_grouped_config` 随后搬到对应分组。迁移完成后下一版本可删除这些顶层键。
