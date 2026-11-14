# 项目架构

## 入口层

`main.py` 负责 AstrBot 生命周期、命令装饰器、公共配置读取和领域对象装配。签到与图片来源的复杂流程由领域 Mixin 实现，入口类直接组合这些 Mixin，不使用动态属性代理。

缓存身份、复用与失效条件、去重及清理机制统一见 [缓存复用、图片去重与幂等](cache-and-dedupe.md)。

## 领域模块

```text
checkin/
│  ├─ models.py          数据模型与成就定义
│  ├─ rules.py           金币、好感度、连签和加持规则
│  ├─ snapshot.py        签到快照校验与版本兼容
│  ├─ schema.py          SQLite 建表与版本迁移
│  ├─ record_store.py    签到资料、奖励与基础背景记录
│  ├─ cache.py           按天 JPEG 缓存与安全规则背景摘要
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
│  ├─ search.py          可选 Lolicon/Pixiv 优先级与双向请求回退流程
│  ├─ search_result.py   命令与工具共享的执行结果
│  ├─ selection.py       本次方向、比例、像素与大小约束
│  ├─ query.py           模式、作品引用与单页目标输入边界
│  ├─ catalog.py         共用作品流、过滤及公开字段投影
│  ├─ discovery.py       查询复用、独立限频与有界内存续页
│  ├─ delivery.py        消息发送错误处理
│  ├─ filters.py         普通分级、漫画和安全策略过滤
│  ├─ safety.py          内置安全词与文本规范化
│  ├─ client.py          Pixiv API 客户端
│  ├─ lolicon.py         Lolicon API 客户端与数据规范化
│  ├─ downloader.py      图片下载、Lolicon 反代轮换与画质选择
│  ├─ index.py           多自然日去重索引、安全词与作品黑名单
plugin_api/
└─ api.py                Plugin Pages 后端 API
```

根目录保留会话策略服务 `group_safety.py`；签到、取图和 Web API 业务分别位于对应包中。根 `__init__.py` 只有模块说明，不注册旧包路径别名；新代码和测试直接导入真实模块。

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
python -m pytest -q tests
```
## 会话内容安全策略

`SessionSafetyService` 分别维护 `group_content_safety_policies` 与 `private_content_safety_policies` 两个配置命名空间。旧 `group_content_safety` SQLite 表只作为群策略的一次性迁移来源；配置保存成功后才备份并将数据库 schema 收敛回 v2，同时永久保留旧表与历史行；保存或收敛失败时保留 v3。新建 v2 数据库不创建旧表。私聊写入不会修改群配置或迁移标记，任一作用域保存失败只回滚该作用域。

每次搜索、签到背景选择、日历背景或已保存在线背景恢复时，入口只解析一次不可变 `ContentSafetyPolicy`，随后传给 Lolicon/Pixiv 取源、本地候选过滤和最终复核。群消息只查询群 ID，私聊只查询发送者用户 ID；缺失记录、无效 ID 或读取异常均使用严格策略，且不跨作用域回退。管理页黑名单缩略图没有会话上下文，因此始终采用严格策略。

会话策略服务隔离群聊与私聊命名空间，通用策略身份同时包含会话类型和标识。签到卡单独使用实际生效的普通分级开关、屏蔽词与作品黑名单生成缓存指纹，不纳入群号或私聊用户标识；未生效的独立列表不影响该指纹。

签到仍按用户和日期保存唯一奖励、内容与基础背景记录。重复签到恢复失败后的重选只更新对应规则下的派生缓存，不覆盖基础记录（包括基础背景画质）。缓存目录按日期保存 JPEG 与原子写入的 JSON 背景摘要；摘要只含模式、来源、作品 ID、标题、作者和画质，不保存原图路径或完整作品元数据。基础记录、主题、渲染档位及实际安全规则共同定位摘要，再按摘要计算 JPEG 键并校验图片，缓存图片与外发作品说明使用同一背景。摘要缺失、损坏或 JPEG 校验失败时安全重建；两者一起随日期过期或由 Pages 清理。主动刷新背景和主题切换更新基础记录及相关缓存 key，后续按新身份查找或生成，不主动清空所有旧变体。管理员预览不写当天正式缓存摘要。

### 内容安全策略来源

签到与付费背景刷新在开始选图前固定本次实际规则列表，全局屏蔽词和作品黑名单由同一条 SQLite 查询读取，后续过滤和缓存指纹使用同一快照，避免管理端并发改规则造成缓存错配。缓存命中时只读背景摘要，不重复写入或同步磁盘；写入摘要与 JPEG 共用活跃日期保护，过期清理会延后处理正在写入的日期目录。

`SessionSafetyService` 持久化策略列表并生成不可变 `ContentSafetyPolicy` 快照；快照缓存身份包含两份独立列表。`FiltersMixin` 根据 `builtin_terms_enabled` 在全局规则源与会话独立规则源之间互斥选择。

调用链为 `SessionSafetyService → ContentSafetyPolicy → FiltersMixin → cache key`；批量操作先构造双 scope 候选，单次保存成功后安装运行时，异常时回滚配置列表、迁移标记和两份运行时快照。

## 发图送达状态

`pixiv/delivery.py` 的 `send_once()` 返回五种状态：消息构建失败为 `FAILED`；调用正常返回为 `CONFIRMED`，但 OneBot file 正常返回且无有效 `message_id`（非零整数或非空白字符串，排除 bool／对象）时为 `ACCEPTED`（已受理，需保留文件）；发送异常链包含超时为 `TIMED_OUT`；其余提交异常为 `UNKNOWN`。超时按 `TimeoutError` 类型或 message/wording 中独立的 timeout/timed out/超时文本判断，先排除路径、URL 和无关回包字段，并保留路径字段后的独立超时诊断（含冒号分隔、无扩展名路径与“上传／读取超时”，不把 timeout.jpg 等文件名当诊断）；文件读取错误也只检查相同诊断文本，不因路径中的 copyfile 重发。包括 OneBot 1200 回包中的 `Timeout: NTEvent … sendMsg`，不能仅凭 1200 判定超时。

`search.py` 对 `CONFIRMED`、`ACCEPTED` 与 `TIMED_OUT` 均记录 normal 使用记录并结算金币；超时不补发，即使最终没有出图也扣费。`FAILED` 与构建期间取消释放未提交图片占用；`UNKNOWN` 与提交期间取消保留 normal_pending 占用，不扣费。OneBot `ACCEPTED`、超时、未知及提交期间取消保留文件读取窗口。构建和发送异常只记日志，不补发异常提示；命令保留正常金币结算消息，工具不外发正常账单。

异常日志只提取 `error_type`、`retcode`、`status`、脱敏 `message/wording`，并包含 `send_mode`、可用的作品 ID、`phase`、`delivery_status` 和 `duration_ms`。错误文本先截取前 2048 字符再脱敏，输出最多 512 字符，限制异常回包的正则处理开销与日志体积；地址、本地路径及 Windows UNC 共享路径、凭据和长数字标识会被遮蔽，不输出完整请求与回包。路径按盘符、UNC、file URI、位于词边界的根目录或点目录起点，以及带文件扩展名的相对路径识别；`NodeIKernelMsgService/sendMsg` 等服务名/方法名保留，不单凭标识内部的斜杠判定路径。file/path/filename 字段支持无引号的空格路径，以后续字段或分隔符为界；引号路径按对应引号闭合，兼容转义斜杠和相对路径后的方括号。编码切换、使用记录、去重占位释放失败分别记录，不重发已确认图片。

## OneBot 图片发送

`send_once()` 在 aiocqhttp 且事件存在 bot 时直接调用已有客户端的 call_action()，不另建连接。单图调用 send_group_msg/send_private_msg，合并转发调用 send_group_forward_msg/send_private_forward_msg，保留 self_id 路由。默认 file URI；构建或提交明确报文件／路径不存在（含 llbot 的“路径不存在”）、权限不足、ENOENT/copyfile 等读取问题时，记录本次异常和 file → base64 切换，再重建同一消息尝试一次。第二次失败也独立记录；发送超时优先分类，绝不触发换编码。其他平台继续使用框架链路。

`onebot_image_transport` 已从 schema 和配置映射移除，真实 AstrBot 加载时按 schema 先裁剪旧键，运行时不依赖其存在。`_cleanup_retired_transport_config()` 只处理未经框架裁剪的残留配置（例如直接传入 dict），删除后尝试保存，保存失败恢复原值；不承担框架裁剪前的迁移，也不能恢复已被裁剪的值。

## 签到卡发送

每日签到（`checkin/application.py`）和付费背景刷新（`checkin/shop.py`）复用 `send_once()`。正常返回或发送超时均继续完成背景使用记录，不补发纯文字，不撤销背景持久化、卡片缓存或去重占用。送达未知或提交期间取消保留已保存背景、卡片及 pending，不写发送成功记录、不补发；`DeliveryNotSubmitted` 表示构建期取消，仍传播取消并释放未提交资源。签到卡在当天缓存中保留，不使用普通 /p 临时文件的 600 秒删除策略；付费刷新未启用卡片缓存时，`ACCEPTED`、超时、未知或提交期间取消的渲染文件交给下载器延迟清理；这是框架 T2I 临时产物，不纳入插件 temp/get_px_* 的重启清理恢复，存活时间仍受框架临时目录策略影响。仅编码时读取的背景源下载文件仍正常清理，不影响已内联的背景与卡片。

明确文件读取失败时使用共享的 file → base64 重试和脱敏诊断日志；发送日志使用 `feature=checkin/checkin_refresh` 标识来源，不在共享发送层声明扣费。确定构建失败保留原来的纯文字回退与清理流程；构建期取消只释放资源并传播取消。奖励在签到写入时发放，付费刷新在发送前完成购买，均不因发送超时回滚。签到测试预览、主题预览及日历发送不在这条持久化卡片流程内。

## 下载临时目录

initialize 获取插件数据目录后，将 `data_dir / "temp"` 注入共享 ImageDownloader。下载器直接在该目录创建 `get_px_*` 文件，未初始化时明确报错，不回退系统临时目录。/p、背景、头像与黑名单缩略图共用该下载器；卡片缓存继续使用 checkin_card_cache，框架 T2I 自行产生的临时文件不在此变更范围。

带有效 `message_id` 的正常返回、base64 正常返回与确定构建失败仍及时清理文件；file 返回 `ACCEPTED`、超时、未知或提交期间取消的 OneBot 发送保留 600 秒，与 Pages 原有的最近10分钟保护保持一致。保留时更新 mtime，并由下载器持有延迟删除任务。close 取消任务但不提前删文件；initialize 在服务开始前扫描插件 temp 的 get_px_*，按 mtime 恢复剩余清理时间，已过期残余立即安排清理，未使用系统临时目录。Pages 仍可清理过期下载文件，不影响数据库。该时间窗口用于读取保护，不代表确认送达。

### OneBot 路径传输的跨用户读权限

下载器 mkstemp 生成的文件在 POSIX 上通常为0600，仅创建用户可读写。在 file 模式序列化图片时，将已下载并校验完成的文件设为0644，支持共享目录中的不同UID读取；该处理同样覆盖合并转发节点。base64、其他平台及Windows不调整文件权限。chmod权限错误记录后切换base64；重建或发送失败仍记录原因，不向聊天发送异常提示。此操作只处理图片文件，不修改共享目录及祖先目录的权限；协议端仍需能遍历这些目录。

## 普通发图的大小筛选

`pixiv/search.py` 在所选画质下下载候选，超出大小上限、地址缺失或下载失败就换作品，先使用同批剩余候选，再按图片源优先级补取。每次请求最多取 3 批、实际尝试 20 张作品，相同 ID 不重复下载；不足时发送已下载的图片。每张候选保留自己的 source_key，补取回退另一源后仍按对应来源写成功记录或释放 pending；跳过候选立即释放，未提交与取消仍由 finally 收尾。

`ImageDownloader.download_for_send()` 的 `allow_quality_fallback=False` 限定普通发图只尝试所选画质的反代和源地址；超软上限时清理文件并抛错交回选图层，50 MiB 硬上限不变。签到及日历传入 `size_limit_bytes=0` 并保留默认画质回退。持久配置沿用 `auto_downgrade_original_mb` 的键与数值，WebUI 名称改为图片大小上限。

初始取源与候选过滤完成后以 `time.monotonic()` 建立 `request_timeout * max(3, count)` 秒的下载及补取截止时间，`count` 为应用数量上限和工具余额限制后的实际执行张数，单张下载（含反代轮换）及补取用 `asyncio.wait_for` 传入剩余预算。到期取消进行中的操作，发送已下载图片；命令的首次取源、消息发送及清理不在这段预算内；工具首次取源另受框架选图预算限制，后续取两个截止时间中较早者。外部取消继续传播。

大小约束由 `download()` 统一执行：Content-Length 超限时不读取正文，流式字节累计超限立即中止并清理半文件；`download_for_send()` 不重复检查返回大小。`ImageSizeLimitError` 按类型记录为 `size_limit` 并交回选图层，异常文案不参与分类。

## 模型取图

`tools/images.py` 统一注册 `get_px_image`、`get_px_context` 及公开查询工具 `get_px_artworks`、`get_px_users`，`skills/get-px-image/SKILL.md` 提供随包调用说明。`main.py` 在初始化后注册工具，卸载时先停用工具、取消并等待在途调用完成结算与清理，再关闭领域资源；生命周期检查覆盖工具解绑。

`SearchMixin._execute_search()` 是命令与工具的共享执行入口，`_handle_search()` 只向命令展示结果消息。`SearchResult` 记录下载、各送达状态、结算和中断事实，不暴露图片路径、直链或正常账单。`ImageSelection` 只在工具请求中生效，先筛候选尺寸，再核对下载文件头，大小限制与管理员限制取较小值。命令已有有限补取和数字 offset 机制保持原样；工具查询通过独立内存游标真实续查，不推进命令游标。

工具从事件取得当前用户和会话，不接受目标会话或安全策略参数；校验完整 UMO 后沿用当前群聊／私聊策略。状态返回 `builtin_terms_enabled`，不提供安全分组或宽松／严格模式。`get_px_context` 不建档、不预留金币，免费时 `balance` 和 `affordable_count` 为 null，不读取钱包。

同事件通过 extra 保存唯一 `SearchResult`；执行中、结束、取消后均不再次取图。工具选图等待预算来自框架超时，预留一半（最多 30 秒）给发送、结算及清理，首轮取源也受限；命令沿用原预算。发送后的结算和清理受 shield 保护，重复取消仍等待收尾；正常返回、受理、超时沿用既有收费规则，未知送达不补发。正常工具账单只记日志，配置上限截断、余额不足缩减或跨低余额阈值时返回提醒。

工具结果通过 `count_limits` 区分配置上限与余额缩减，保留原始请求数量、实际执行数量及本次配置上限；两类限制叠加时合并提醒，正常结算后的低余额提示不覆盖数量缩减原因。结算调用异常保持 `settlement=unknown`，日志只说明扣费状态待核对，不承诺未扣费。

## 模型公开查询与发送

四个原生工具由同一个服务注册与关闭。默认取图保留旧图源路径；明确模式通过领域作品流获取候选，再进入共享下载、发送、结算和清理链路。榜单、搜索、作者和关联作品保持上游顺序；指定作品使用独立的临时占用命名空间，忽略完成历史但不覆盖历史记录，失败释放临时占用，送达未知保留占用。

公开查询不写图片使用或命令分页记录。真实续页解析固定 SDK 端点的白名单参数，内存状态保留未展示候选；游标绑定会话、用户、账号配置及实际安全规则，10分钟过期、最多128个，重载清空。同事件相同查询复用 Future，查询限频与发图分离；每次最多扫描3个上游批次，并受 request_timeout 与框架超时中较小者约束。Lolicon 没有真实续页。参数及状态集中在[随包 Skill](../../skills/get-px-image/SKILL.md)。

关联接口兼容 `seed_illust_ids[0]`、`viewed[0]` 等连续索引数组，保留上游已浏览作品位置。SDK 高层关联方法未暴露 `viewed`，对应续页通过 SDK 的认证请求方法访问同一个固定关联端点，不直接请求上游返回的 URL。
