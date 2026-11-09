# 配置说明

配置真源是根目录 `_conf_schema.json`，已按功能分为 6 个 object 分组。插件管理中心的“插件设置”按这些分组编辑全部可见普通配置；全局安全规则（`global_safety_terms`、`keyword_match_mode`、`global_blacklisted_illust_ids`）、`safety_groups` 与群聊／私聊模板列表仍由“安全分组”编辑器（原“会话策略”）独占读写，设置页只提供跳转入口，通用设置接口拒绝写入这些键，避免双重写入。修改配置字段时必须同步更新 README、本文档和相关测试。

在管理中心更改常规设置后多数选项下次请求即生效；Pixiv 令牌、图片反代、Lolicon 排除 AI、万象画卷联动与下载超时需重载插件后完整生效，字体来源需重新载入管理中心页面。Pixiv refresh token 的读取接口仅返回是否已配置；密码框留空表示保持不变，输入新令牌替换，清除必须走独立的 `clear_secrets` 请求字段（页面上的“清除已保存的令牌”），空文本不会被当作清除，令牌值不进入快照默认值或日志。AI 问候模型通过公开的 `context.get_all_providers()` 枚举为 id＋模型标签；枚举失败时保留已保存的 ID 并显示提示，不猜字段名、不静默清空。去重天数在发图与模型取图入口使用同一份配置。

## 1. Pixiv 图源与下载（`pixiv_source`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `pixiv_refresh_token` | `string` | 空 | 使用 Pixiv 作为首选或回退时需要填写；留空直接使用 Lolicon |
| `preferred_image_source` | `enum` | `Lolicon` | 下拉选择 `Lolicon` / `Pixiv`；首选请求失败或无结果时尝试另一源 |
| `lolicon_exclude_ai` | `bool` | `true` | 仅向 Lolicon API 传递 excludeAI=true/false；R18 是否混入由安全分组的「允许 R18」决定，未分组默认仅普通分级 |
| `lolicon_image_proxy_origins` | `text` | 空 | 每行一个 http(s) origin，最多 5 个按序尝试；仅改写允许列表内 Pixiv 图片主机 |
| `max_count` | `int` | `5` | 单次指令最多发送张数，范围 1–20；模型取图数量同样受此上限截断 |
| `p_coin_cost` | `int` | `20` | `/p` 每张金币，范围 0–200；0 免费，关闭签到后不扣费；正常返回与发送超时均扣费，下载／构建失败及其他发送异常不扣费 |
| `image_quality` | `enum` | `original` | `original`/`large`/`medium`；固定所选画质，超限／不可用时换作品 |
| `auto_downgrade_original_mb` | `float` | `10.0` | 图片大小上限（MiB），适用于所选画质，范围 0–25；0 关闭软上限，仍有 50 MiB 硬上限；保留旧键并沿用已有数值 |
| `forward_threshold` | `int` | `1` | 下载张数严格大于此值时合并转发（仅 aiocqhttp）；0 始终合并，范围 0–20；模型取图默认 `single` 逐张直发，仅显式 `auto` 才用此阈值 |
| `auto_trigger_enabled` | `bool` | `false` | 整条消息形如「来三张初音ミク图」时自动触发发图；私聊同样生效 |

`preferred_image_source` 下次请求即生效，普通发图、签到背景与日历背景共用请求顺序。Lolicon 有标签时搜索、无标签时随机；Pixiv 有标签时搜索、无标签时推荐。无 Pixiv token 时直接使用 Lolicon；两源均失败或无结果时按各入口现有失败路径处理。单张候选被过滤、去重或下载失败不直接触发换源；普通发图耗尽本批可用候选后按相同优先级补取，请求失败或空结果时仍可回退另一源；有效缓存及已保存背景继续复用，不因切换优先级强制重选。无效选项记录日志并使用默认 Lolicon 优先。

普通发图先用同批剩余候选补足张数，首批全被漫画、安全规则或去重过滤时也继续补取。每次请求最多取 3 轮候选（包含首轮，每轮最多尝试两个来源，并非最多 3 次 HTTP 请求）、实际尝试下载 20 张作品，同一原始作品 ID 不重复下载。安全规则读取和去重索引异常停止选图，不当作候选不足继续放行。大小等于上限可发送；超限、所选画质地址缺失或下载失败则换图，不尝试其他画质。反代与源地址重试保持同一画质。跳过候选清理文件并释放临时占用，最终按实际发送调用结果结算；不足时发送已找到的图片。签到和日历仍使用独立画质规则，不应用此软上限。

首轮取源完成后，下载和候选补取共享 `request_timeout × max(3, 请求张数)` 秒预算；默认超时 30 秒时，1 张为 90 秒、5 张为 150 秒。预算到期会取消进行中的网络操作，发送已下载图片，允许少于请求张数；首次取源、消息发送和资源清理不计入该预算。大小上限在响应头及分块读取时执行。

Lolicon 补取是重新随机请求，不支持 offset 或 continuation，不更新分页索引，结果可能重复或少于指定数量。Pixiv 在请求内保留真实下一页参数，空页有 continuation 时可继续，没有下一页或位置重复时不绕回首页；跨请求仍以已有数字 offset 定位，推荐接口的完整多参数位置只保留在请求内。签到配置标签时，Pixiv 搜索使用竖图粗筛并继续本地 0.60–0.90 精筛；无标签推荐不套用搜索参数，日历仍只请求一轮。两源异常响应、缺失列表及无效候选字段明确拒绝，不作为合法空结果；未增加 AI 配置或额外 429 重试。

原生 `get_px_image` 可附加请求级 `orientation`、`aspect_ratio`／`ratio_tolerance`、`min_width`／`min_height` 和 `max_size_mib`，不新增持久配置或改变六组 schema。`max_size_mib` 与 `auto_downgrade_original_mb` 的有效正数上限取较小值，管理员0时仍可收紧到本次上限，50MiB硬上限不变；省略沿用配置。像素要求按实际发送文件核对，不升降画质；方向／比例／像素筛选后有限补取，不放宽。参数定义与示例见 [模型原生取图](../../README.md#模型原生取图)。这些参数不扩展 `/p` 签名，也不影响签到／日历。Tool默认／省略`send_mode`为`single`，不应用合并阈值；只有显式`auto`才使用`forward_threshold`。模型仅用户明确要求合并时选择`forward`，明确要求按配置自动选择时才用`auto`，`/p`仍沿用原阈值。

旧阈值不会自动改为 10 MiB；使用原图且频繁因大小限制换图时，建议手动将上限调整为 10 MiB。

Lolicon 请求地址固定为 `https://api.lolicon.app/setu/v2`。旧 `lolicon_api_url` 从 schema 删除，由 AstrBot 加载时裁剪；简单 dict 中的残留值也不参与请求，旧自定义地址和留空停用不再生效。图片反代 origin 配置仍只代理图片，不代理 API：每行一个完整 origin，最多 5 个按顺序尝试；拒绝带账号密码、路径、query 或 fragment 的地址，重复项自动删除；只改写 Lolicon 来源且位于允许列表中的 Pixiv 图片主机，全部失败后回退 API 返回地址；留空表示不改写，需重载插件生效。

OneBot 发图默认使用本地 `file:///` 路径；仅在明确文件读取失败时，将同一条消息回退到 base64 编码重试一次，超时不回退、不重发。base64 是图片编码，与 AstrBot 4.27.3 的 OneBot 默认发图编码相同；回退仍直接调用 OneBot，不额外尝试 AstrBot 框架发送。file 发送正常返回但没有有效 `message_id` 时仍扣费，并保留图片 10 分钟供协议端读取，不重发。旧 `onebot_image_transport`（顶层及 `pixiv_source` 内）由 AstrBot 在加载时按 schema 裁剪；插件不依赖旧键，也不能恢复框架已裁剪的值。发送异常仅记日志，详见 [OneBot 图片发送](../../README.md#onebot-图片发送)。

## 2. 图片筛选与去重（`content_dedupe`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `filter_manga` | `bool` | `true` | 过滤 Pixiv 结果中的 manga；Lolicon 返回按插画处理 |
| `dedupe_days` | `int` | `1` | 最近 0–7 个北京时间自然日；1 仅当天，非滚动 24 小时；同群共享，不同会话、标签和图源隔离；0 关闭并清空使用记录 |
| `global_safety_terms` | `list` | `[]` | 全局屏蔽关键词；与分组关键词叠加 |
| `keyword_match_mode` | `string` | `宽松` | 下拉选择宽松／严格，对所有关键词生效；不检查简介，不支持正则或通配符 |
| `global_blacklisted_illust_ids` | `list` | `[]` | 每项一个正整数 ID 字符串，所有会话生效；同 PID 分页图片也拦截 |
| `safety_groups` | `template_list` | `[]` | 名称、完整 UMO 列表、允许 R18、附加关键词 |
| `safety_rules_version` | `int` | `0` | 隐藏迁移版本；初始化完成后为 1，防止旧库规则复活 |

`dedupe_days` 不控制签到 JPEG、日历或预览缓存的有效期。窗口缩短会清理超期使用记录，扩大不能恢复已删除历史；保存后下一次发图请求即同步，发图与模型取图入口使用同一份配置，无需重载。查询、预占用及不同入口的复用规则见 [缓存与去重](cache-and-dedupe.md#2-正式图片去重)。

## 3. 万象画卷联动（`checkin_omnidraw`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `checkin_omnidraw_link_enabled` | `bool` | `false` | 联动总开关；开启后才会检测已安装的万象画卷插件，启用商店生图额度、签到发放额度、`/签到状态` 额度展示等全部联动功能 |
| `checkin_omnidraw_quota_cost` | `int` | `75` | 生图额度每张单价，范围 0–300；实际花费 = 单价 × 张数；0 免费 |
| `checkin_omnidraw_quota_default` | `int` | `1` | 不指定张数时默认购买张数，范围 1–50 |
| `checkin_omnidraw_quota_daily_max` | `int` | `10` | 每人每日购买上限，0 表示不限（最大 30）；签到赠送不受此限 |

仅在 `checkin_omnidraw_link_enabled` 开启、且实际安装万象画卷并启用其每日生图限制时生效。购买张数 `签到商店 生图 <张数>`（1–50），额度当日有效，跨天清零。

## 4. 签到基础（`checkin_basic`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `checkin_enabled` | `bool` | `true` | 签到总开关；关闭后商店、主题与刷新背景不可用，`/p` 与模型取图也不再扣金币 |
| `checkin_bot_name` | `string` | `neko` | 签到卡片 bot 角色名 |
| `checkin_card_quality_tier` | `enum` | `省流量` | `省流量`(960×540+medium)/`清晰`(1248×702+large)/`极致`(1728×972+large)；日历同档但尺寸分别为 1600×900／2080×1170／2880×1620；普通重复签到保持当天记录档位 |
| `checkin_avatar_enabled` | `bool` | `true` | QQ 平台尝试在卡片显示用户头像 |
| `checkin_greeting_mode` | `enum` | `hitokoto` | `local`(本地事件)/`hitokoto`(一言 API)/`ai`(文本模型)；请求或校验失败回退本地文案 |
| `checkin_hitokoto_categories` | `list` | `["全部"]` | 一言类型多选 |
| `checkin_ai_greeting_provider_id` | `string` | 空 | AI 问候模型；管理中心按可用模型枚举 id 与模型标签，留空用当前会话模型，仍不可用则回退本地文案。发送给模型的内容只有昵称、日期、签到统计、关系阶段、奖励、称号和成就等展示数据，不发送用户 ID，昵称不可用时使用“匿名用户” |
| `checkin_ai_greeting_prompt` | `text` | 见 schema | AI 问候提示词，可自定义角色语气；插件仍追加不可覆盖的输出与数据边界规则 |
| `checkin_ai_greeting_timeout` | `float` | `8.0` | AI 问候超时，1–30 秒 |
| `checkin_hitokoto_timeout` | `float` | `5.0` | 一言请求超时，1–15 秒 |
| `checkin_background_mode` | `enum` | `pixiv_daily` | `pixiv_daily`(每日自动选背景)/`custom`(固定背景文件) |
| `checkin_background_tag` | `string` | 空 | 多标签随机排列逐个尝试，使用记录按最终标签隔离；留空按所选优先级请求 Lolicon 随机或 Pixiv 推荐作品 |
| `checkin_custom_background` | `string` | 空 | 管理员本地背景路径，推荐 3:4 竖图，支持 jpg/png/webp；相对路径按插件数据目录解析 |

## 5. 签到商店与定价（`checkin_shop`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `checkin_background_refresh_cost` | `int` | `100` | 签到后刷新在线背景的金币，范围 0–300；0 免费；仅 `pixiv_daily` 模式可用 |
| `checkin_theme_cost` | `int` | `1500` | 非默认签到主题价格，范围 0–5000；默认「米白」始终免费 |

`checkin/themes.py` 的 `price` 仅作读取失败的兜底。主题编号：`00` 米白(免费)、`01` 浅蓝、`02` 红黑、`03` 黄黑、`04`–`07` 四季系列(新柳/荷风/丹枫/寒梅)。`/签到主题 查看|购买|切换 <编号>` 支持编号、ID、中文名。

## 6. 运行参数（`runtime`）

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `request_timeout` | `float` | `30.0` | 图片下载与图源 API 超时，5–120 秒；发图总预算为 `request_timeout × max(3, 请求张数)` 秒 |
| `rate_limit_seconds` | `int` | `3` | 同一用户两次发图请求（`/p` 与模型取图）的最小间隔，0 禁用，0–60 秒 |
| `webui_font_source` | `enum` | `mirror` | 插件管理中心 Google Fonts 加载方式：`mirror`(国内镜像)/`official`/`none`；改动后需重新载入管理中心页面 |

## 会话内容安全策略

安全配置统一位于 `content_dedupe`；配置页与 Pages 修改同一份配置，不再从 SQLite 读取运行时关键词／黑名单。

每个 `safety_groups` 条目包含 `name`、`sessions`、`allow_r18=false`、`custom_terms=[]`。隐藏 `group_id` 由服务生成并持久化，只用于稳定编辑身份。`sessions` 使用 `/sid` 返回的完整 `平台实例:消息类型:会话ID`；一个 UMO 只能归属一个分组，不猜测裸群号／用户号对应哪个 Bot。

管理中心的「新建分组」和分组卡片共用居中圆角弹窗；会话、附加关键词按行输入，「允许 R18」使用默认关闭的开关。小屏内容可滚动，操作栏保留在底部；保存失败保留草稿，放弃未保存修改前需确认。

- 未分组或无会话上下文：全局规则＋R18 关闭。有效分组：全局词＋组词，全局作品 ID 黑名单始终生效。
- 只有一个全局匹配模式。宽松匹配完整原标签与翻译标签；严格另外匹配标题子串。查询词在宽松模式完整匹配、严格模式按子串预检；均不执行正则／通配符、不检查简介。
- NFKC、casefold、首尾空白规范化；词内空格、连字符等分隔符不删除。关键词最多 64 字符；作品 ID 必须为 ASCII 正整数文本，前导零归一化。
- 分组 R18 开启表示允许混合分级候选，不是只检索 R18。分级、词、ID 同时校验；规则读取／校验失败拒绝请求。
- 旧群／私聊编辑和批量复制 API 已删除。保存失败恢复内存配置及列表引用，后续请求不使用未保存草稿。

## 升级基线

配置升级基线仍为已完成迁移的 v3.8.0，顶层仅保留六个正式分组。旧小时去重与扁平配置迁移不恢复。`content_dedupe` 中隐藏保留旧 `group_content_safety_policies`、`private_content_safety_policies` 和旧迁移标记，仅供归档；旧模板和运行逻辑已删除。不能先删桥接键：AstrBot 在插件初始化前会裁剪 schema 外字段。

首次初始化按内容哈希原子写入 `safety_legacy_backups/legacy-<sha256>.json` 并读回核对，归档旧配置、历史数据库策略、全局词及作品元信息。核对成功才保存版本 1 并清空旧配置列表；失败停止加载，不猜 UMO 或继承旧 R18。旧全局人工词与作品 ID 合并进新配置，旧局部列表只归档。版本 1 后不再读旧来源，删除的新规则不会重载复活。SQLite 历史策略表保留为只读历史，schema3 在配置保存后经备份收敛回 v2，不删除签到／金币／去重数据。

管理中心数据导出包含 `checkin`、`safety`、`blacklist_metadata` 和 `legacy_safety_archives`，不包含凭据。导入包只恢复签到并提前创建回滚备份；当前安全配置及旧档案均不导入。旧签到 JSON 仍可导入，旧安全档案不能单独恢复。

升级前备份插件配置 JSON 和数据目录。早于 v3.8.0 的版本须先安装 v3.8.0、成功启动，并确认自定义值及群／私聊策略已保存到对应分组后再升级。仅存在于顶层的值不属于支持范围；AstrBot 4.27.3 在插件初始化前删除 schema 外的键，插件无法在裁剪后找回旧值。若来自 v2.8.x 或已有数据但数据库无 schema 版本号，先使用 v3.0.0 完成签到数据迁移，再经过 v3.8.0 完成配置迁移。不要先升级再依赖回退旧版本恢复配置。

## 配置维护规则

- README 配置表必须和 `_conf_schema.json` 保持一致。
- 运行时通过 `main.py` 的 `_cfg_str`/`_cfg_int`/`_cfg_float`/`_cfg_bool` 读取。正式配置由 AstrBot 按分组加载；读取层保留简单 dict 支持，但不承担旧扁平配置迁移。不要在业务流程散落 `self.config.get(...)`。
- 分组后存盘为嵌套 `config[组][键]`；新增配置项时加入对应组的 `items`，读取层无需改动。
- 删除、重命名或改字段类型时必须说明兼容影响。
- `forward_threshold` 按下载张数判断，仅 aiocqhttp 合并转发；旧 `send_as_forward` 仅在新字段缺失时兼容（`true`→0，`false`→20）。
- `dedupe_days` 缩短天数会在下一次发图请求清理超期记录，增加天数无法恢复已清理历史。
- `image_quality` 不影响签到背景，签到卡/日历背景画质由 `checkin_card_quality_tier` 独立控制。
- `pixiv_refresh_token` 留空时跳过 Pixiv，仅请求 Lolicon；失败或无结果时不再有可用回退源。
- schema 顶层仅定义六个正式分组。安全服务只读写 `content_dedupe`；嵌套旧桥接键仅归档，空列表不恢复已停用的顶层策略。
