# 项目架构

## 入口层

`main.py` 负责 AstrBot 生命周期、命令装饰器、公共配置读取和领域对象装配。签到与图片来源的复杂流程由领域 Mixin 实现，入口类直接组合这些 Mixin，不使用动态属性代理。

## 领域模块

```text
checkin/
│  ├─ models.py          数据模型与成就定义
│  ├─ rules.py           金币、好感度、连签和加持规则
│  ├─ snapshot.py        签到快照校验与版本兼容
│  ├─ schema.py          SQLite 建表与版本迁移
│  ├─ record_store.py    签到资料、奖励和卡片记录
│  ├─ feature_store.py   生日、成就、称号和全局事件
│  ├─ backup_store.py    快照导入导出
│  ├─ ranking_store.py   群签到排行与趋势查询
│  ├─ store.py           CheckinStore 组合入口
│  ├─ application.py     每日签到流程与问候内容
│  ├─ commands.py        签到功能命令业务
│  ├─ shop.py            商店商品目录、购买处理与付费背景刷新
│  ├─ artwork.py         卡片渲染与背景作品选择
│  └─ holiday.py         联网节假日数据更新与查询
pixiv/
│  ├─ search.py          Lolicon 主源与 Pixiv 搜索/推荐回退流程
│  ├─ delivery.py        消息发送错误处理
│  ├─ filters.py         普通分级、漫画和安全策略过滤
│  ├─ safety.py          内置安全词与文本规范化
│  ├─ client.py          Pixiv API 客户端
│  ├─ lolicon.py         Lolicon API 客户端与数据规范化
│  ├─ downloader.py      图片下载、Lolicon 反代轮换与质量降级
│  ├─ index.py           多自然日去重索引、安全词与作品黑名单
plugin_api/
└─ api.py                Plugin Pages 后端 API
```

根目录不再保留单文件业务实现或兼容 wrapper。`__init__.py` 集中注册旧包路径别名，已有预览脚本可以继续使用，但新代码和测试必须直接导入领域包。

模块按职责划分，不按行数强制拆分。`__init__.py` 和组合入口可以很小；普通业务模块只有在具备独立职责和测试边界时才单独存在。

## 签到指令入口

签到业务使用 `/签到`、`/签到帮助`、`/签到状态`、`/签到成就`、`/签到日历`、`/刷新背景` 顶层入口，配合 `签到生日`、`签到称号`、`签到排行`、`签到商店`、`签到主题`、`签到管理` 六个指令组；纯文本“签到”继续由正则触发。各指令组的根路径由 AstrBot 显示对应指令树；`/签到帮助` 是独立普通指令，仅发送完整帮助图。旧 `签到中心` 路由和平铺业务指令不再保留。指令装饰器集中在 `main.py`，处理函数继续委托给 `CheckinCommandMixin` 与 `CheckinShopMixin`，避免命令结构与业务实现相互耦合。

## 签到商店扩展

`checkin/shop.py` 中的 `build_checkin_shop_items()` 是商品展示目录的统一注册点，每个商品拥有稳定 `item_id`、分类、指令、名称和价格。新增商品时先在目录中注册展示项，再在 `CheckinShopMixin` 增加购买处理，并将需要原子扣款的数据操作放入 `CheckinStore` 对应 store 模块；入口层只保留 AstrBot 指令装饰器。商品目录和购买行为应分别补充测试。

万象画卷生图额度（`item_id="omnidraw:quota"`）是首个条件商品：仅在总开关 `checkin_omnidraw_link_enabled` 开启且 `plugin._omnidraw_bridge`（`checkin/omnidraw_bridge.py`，initialize 中按开关创建）可用、快照 `available` 时注入目录；购买流程先经 `spend_coins` 扣款，再调用 bridge `grant` 发放，发放失败时用 `add_coins` 原路退回。

## 签到卡主题模板

每个主题位于 `templates/checkin_themes/<theme_id>/`，并提供 `style.css` 和 `preview.png`；默认、蓝、红、黄主题仍各自提供完整的 `index.html`。四季主题共用 `_shared/index.html` 和 `_shared/layout.css`，主题目录只保留配色样式与 `artwork.svg`，`get_checkin_card_template()` 在运行时将共享壳、主题 SVG 和 CSS 拼成最终自包含 HTML。最终模板会把 CSS 内联进 HTML 的 `/*__CHECKIN_CARD_CSS__*/` 标记，并将字体 base64 填入 `__CHECKIN_CARD_FONT_DATA__`，因此输出不得引用任何外部 URL 或跨目录资源；`test_all_registered_checkin_themes_are_self_contained` 会校验这一点。主题注册在 `checkin/themes.py`，模板内容变化时应同步 `version`，它参与签到卡缓存 key；共享四季壳或布局变化时，需要同步检查四季主题版本和缓存影响。

四季系列（`spring`/`summer`/`autumn`/`winter`）共用同一套布局 class（`.season-card`、`.season-info`）和信息区块，只有调色板、季节文案和装饰 SVG 不同。正式主题目录只保留共享壳所需的 `style.css`、`artwork.svg` 和最终 `preview.png`。开发期的构建脚本、预览渲染工具和测试素材只在本地工作区使用，不参与插件运行时和正式提交；改动装饰参数或预览素材后，应在本地重新生成对应正式目录中的 `preview.png`。

## 前端页面

`pages/pluginCenter/` 使用原生 HTML、CSS 和 ES module，集中提供群排行、成员当前数值编辑、内容安全、会话策略和签到数据管理。会话策略由配置文件持久化；签到业务数据仍由 SQLite 和签到备份管理。成员编辑只更新 `checkin_users`，不回写 `checkin_records` 或 `checkin_group_presence`。

## 依赖方向

```text
main.py
  → checkin / pixiv / plugin_api
    → rules / stores / renderers / clients
      → SQLite、文件系统、Lolicon、Pixiv、AstrBot、Hitokoto
```

数据模型和规则不依赖 AstrBot 事件对象。AstrBot 事件、消息链和 Plugin Pages bridge 只出现在入口、服务与 Web API 层。

## 验证

```powershell
python -m json.tool _conf_schema.json
python -m compileall -q main.py checkin pixiv plugin_api scripts/ci tests
node --check pages/pluginCenter/app.js
python -m pytest -q
```
# 会话内容安全策略

`SessionSafetyService` 分别维护 `group_content_safety_policies` 与 `private_content_safety_policies` 两个配置命名空间。旧 `group_content_safety` SQLite 表只作为群策略的一次性迁移来源；配置保存成功后才备份并将数据库 schema 收敛回 v2，同时永久保留旧表与历史行；保存或收敛失败时保留 v3。新建 v2 数据库不创建旧表。私聊写入不会修改群配置或迁移标记，任一作用域保存失败只回滚该作用域。

每次搜索、签到背景选择、日历背景或已保存在线背景恢复时，入口只解析一次不可变 `ContentSafetyPolicy`，随后传给 Lolicon 取源、Pixiv 回退、本地候选过滤和最终复核。群消息只查询群 ID，私聊只查询发送者用户 ID；缺失记录、无效 ID 或读取异常均使用严格策略，且不跨作用域回退。管理页黑名单缩略图没有会话上下文，因此始终采用严格策略。

会话策略服务隔离群聊与私聊命名空间，通用策略身份同时包含会话类型和标识。签到卡单独使用实际生效的普通分级开关、屏蔽词与作品黑名单生成缓存指纹，不纳入群号或私聊用户标识；未生效的独立列表不影响该指纹。

签到仍按用户和日期保存唯一奖励、内容与基础背景记录。重复签到恢复失败后的重选只更新对应规则下的派生缓存，不覆盖基础记录（包括基础背景画质）。缓存目录按日期保存 JPEG 与原子写入的 JSON 背景摘要；摘要只含模式、来源、作品 ID、标题、作者和画质，不保存原图路径或完整作品元数据。基础记录、主题、渲染档位及实际安全规则共同定位摘要，再按摘要计算 JPEG 键并校验图片，缓存图片与外发作品说明使用同一背景。摘要缺失、损坏或 JPEG 校验失败时安全重建；两者一起随日期过期或由 Pages 清理。主动刷新背景和主题切换改变基础记录，使相关旧变体无法命中。管理员预览不写当天正式缓存摘要。
### 内容安全策略来源

签到与付费背景刷新在开始选图前固定本次实际规则列表，全局屏蔽词和作品黑名单由同一条 SQLite 查询读取，后续过滤和缓存指纹使用同一快照，避免管理端并发改规则造成缓存错配。缓存命中时只读背景摘要，不重复写入或同步磁盘；写入摘要与 JPEG 共用活跃日期保护，过期清理会延后处理正在写入的日期目录。

`SessionSafetyService` 持久化策略列表并生成不可变 `ContentSafetyPolicy` 快照；快照缓存身份包含两份独立列表。`FiltersMixin` 根据 `builtin_terms_enabled` 在全局规则源与会话独立规则源之间互斥选择。

调用链为 `SessionSafetyService → ContentSafetyPolicy → FiltersMixin → cache key`；批量操作先构造双 scope 候选，单次保存成功后安装运行时，异常时回滚配置列表、迁移标记和两份运行时快照。

## 发图送达状态

`pixiv/delivery.py` 的 `send_once()` 对普通图片和合并转发返回 `DeliveryStatus` 三态：构建异常为 `FAILED`，提示未发送；OneBot 直调须取得含 `message_id` 的 data 回执才为 `CONFIRMED`，框架发送沿用其正常返回语义；空回执、异步受理或发送异常为 `UNKNOWN`。aiocqhttp 已对失败协议回包抛出异常并解包 data，调用方不把原始 status/retcode 当作成功回执。协议报错不保证图片未送达，不能简单归为确定失败。仅确认成功的作品进入使用记录与金币结算；未知不扣费、不重发、不从转发降为逐条补发。此策略没有查询 QQ 实际送达状态。

`search.py` 按三态分别处理：构建失败、构建期间取消及构建失败通知期间取消均释放未提交图片的占用；确认成功记录使用并结算；未知与提交期间取消保留原 `normal_pending` 占用（仅在开启去重时生效），随自然日去重窗口过期。未知的 file 图片交给下载器延迟清理，base64 图片无需保留原文件。通知发送失败独立记录日志，不触发重发图片。

异常日志只提取 `error_type`、`retcode`、`status`、脱敏 `message/wording`，并包含 `send_mode`、可用的作品 ID、`phase`、`delivery_status` 和 `duration_ms`。错误文本最多 512 字符，避免异常回包撑大日志；地址、本地路径及 Windows UNC 共享路径、凭据和长数字标识会被遮蔽，不输出完整请求与回包。错误提示、使用记录、去重占位释放失败分别记录，不重发已确认图片。

## OneBot 图片发送

`send_once()` 在 aiocqhttp 且存在事件 `bot` 时，根据 `onebot_image_transport` 选择直接调用事件已有客户端的 `call_action()`，不另建连接。单图调用 send_group_msg/send_private_msg，合并转发调用 send_group_forward_msg/send_private_forward_msg，保留 self_id 路由。file 模式显式转换图片为本地 URI，base64 模式沿用图片组件编码；两者均直接组装文本、图片和转发节点。astrbot 模式或其他平台继续走原框架链路。发送结果未知时不自动重发、不计费；传输模式由配置明确指定，发送异常后不自动切换模式。

## 下载临时目录

initialize 获取插件数据目录后，将 `data_dir / "temp"` 注入共享 ImageDownloader。下载器直接在该目录创建 `get_px_*` 文件，未初始化时明确报错，不回退系统临时目录。/p、背景、头像与黑名单缩略图共用该下载器；卡片缓存继续使用 checkin_card_cache，框架 T2I 自行产生的临时文件不在此变更范围。

确认成功与确定构建失败仍及时清理文件；未知或提交期间取消的 file 发送保留 600 秒，与 Pages 原有的最近10分钟保护保持一致。保留时更新 mtime，并由下载器持有延迟删除任务。close 取消任务但不提前删文件；initialize 在服务开始前扫描插件 temp 的 get_px_*，按 mtime 恢复剩余清理时间，已过期残余立即安排清理，未使用系统临时目录。Pages 仍可清理过期下载文件，不影响数据库。该时间窗口用于读取保护，不代表确认送达。

### OneBot 路径传输的跨用户读权限

下载器 mkstemp 生成的文件在 POSIX 上通常为0600，仅创建用户可读写。在 file 模式序列化图片时，将已下载并校验完成的文件设为0644，支持共享目录中的不同UID读取；该处理同样覆盖合并转发节点。base64和astrbot模式以及Windows不调整文件权限。chmod失败按构建阶段错误记录，不继续调用发送API，不自动补发。此操作只处理图片文件，不修改共享目录及祖先目录的权限；协议端仍需能遍历这些目录。
