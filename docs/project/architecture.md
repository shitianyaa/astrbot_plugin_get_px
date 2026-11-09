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
│  ├─ search.py          命令／Tool 共用取图、发送、固定价格结算与清理
│  ├─ search_result.py   执行事实摘要，不携带图片路径／URL／正常账单
│  ├─ selection.py       请求级方向／比例／像素／大小校验与实际文件尺寸复核
│  ├─ delivery.py        消息发送错误处理
│  ├─ filters.py         普通分级、漫画和安全策略过滤
│  ├─ safety.py          有效规则快照与关键词匹配
│  ├─ client.py          Pixiv API 客户端、签到标签竖图粗筛
│  ├─ pagination.py      请求内分页状态、上游响应与 continuation 校验
│  ├─ lolicon.py         Lolicon API 客户端与数据规范化
│  ├─ downloader.py      图片下载、Lolicon 反代轮换与画质选择
│  ├─ index.py           多自然日去重索引、黑名单辅助元信息与旧词归档读取
plugin_api/
├─ api.py                Plugin Pages 后端 API 与设置路由
└─ settings.py           Schema 驱动的设置快照、委派视图、校验和部分保存
tools/
└─ images.py             原生 Tool、同事件幂等与在途取消生命周期
skills/get-px-image/
└─ SKILL.md              随包取图使用说明
```

根目录保留会话策略服务 `group_safety.py`；签到、取图和 Web API 业务分别位于对应包中。根 `__init__.py` 只有模块说明，不注册旧包路径别名；新代码和测试直接导入真实模块。

管理中心普通配置由 `plugin_api/settings.py` 从 `_conf_schema.json` 生成界面目录，读取当前有效分组/旧扁平值并在提交时严格验证；敏感令牌在读取时脱敏。全局关键词、匹配方式与作品黑名单委派到“内容安全”，安全分组与群/私聊模板列表委派到“安全分组”，均不开放通用配置覆盖接口。带选项的多选会返回已保存的旧值供页面勾选保留，写入只接受声明选项或该键当前已存值，不会把旧值扩成新选项。设置页读取或保存期间冻结所有会改变草稿的控件（输入、开关、下拉、搜索、分组导航、手机组选择、清除令牌、放弃与重新载入），结束后恢复各控件原有 disabled 状态；保存失败保留草稿，保存成功不回读、不自动重载插件或页面。

模块按职责划分，不按行数强制拆分。`__init__.py` 和组合入口可以很小；普通业务模块只有在具备独立职责和测试边界时才单独存在。

## 原生模型取图

初始化成功后 `ImageToolService` 注册 `get_px_image`／`get_px_context`，显式将 `handler_module_path` 绑定实际插件入口，以兼容旧框架模块解析；终止时先停新请求，取消并等待在途工具，再清理客户端／索引。仅按对象身份移除本实例注册的工具，不删除同名外部对象。

`SearchMixin._execute_search()` 返回 `SearchResult`，`_handle_search()` 保留命令 yield 展示。两个入口复用同一选图、发送、结算和 finally 清理；收费开关、单价和转发阈值在执行开始固定。Tool 仅收费模式使用 `find_profile()` 查询余额，未知用户按零余额不建档，故障拒绝；签到关闭或单价为 0 时不读钱包，状态返回未查询的 `null`；`/p` 既有预检故障跳过行为不扩改。成功结算低余额提醒用 `spend_coins()` 返回的事务后余额与扣款额反推同事务前余额，不用预检值判断。

Tool 只回文本事实，图片由插件发送，不返回图片内容或路径。`event` 内只记录本次执行结果和在途状态，重复调用不再消费；追加取图需新事件，不为框架合并 follow-up 猜测授权。选图截止时间受实际 Tool 超时约束，余留一半（最多30秒）给发送/结算/清理；不后台化、不改全局超时。取消继续传播，已开始的结算/清理通过持有并等待的任务完成，保持回执、占用和文件保护期。

Tool 标签硬上限 48 字符，超长拒绝、不截断；说明约束模型只选用户原名或一个确定的译名，不擅自扩写，不在代码中识别角色或猜译名。`send_mode=auto/single/forward` 只覆盖当前 Tool 调用的发送选择：默认／省略为single，不合并；仅用户明确要求合并时模型才选择forward，仍受 OneBot 平台限制；显式auto沿用执行开始固定的阈值，仅用户明确要求按配置自动选择时使用。不会写回配置，命令仍按原阈值；换方式的重复调用也受同事件幂等保护，未知送达不补发。

Tool 附加不可变 `ImageSelection`：方向、绝对容差比例、最小宽高在元数据过滤后、claim 前执行；下载成功后只读文件头核对实际发送档位尺寸，不解码画面、不调用视觉模型。缺失／无效宽高在有尺寸要求时拒绝；不符合的文件及时清理、释放 pending 并使用同批候选补足。无附加条件时保持既有行为。`max_size_mib` 只收紧配置上限，省略不改变配置的0语义；50MiB硬上限由下载器继续执行。比例与方向叠加，不偷偷放宽，不扩展候选浏览或主动翻页工具。Tool 部分找到时返回实际数量说明，安全／索引故障仍保留服务故障提示；筛选不改变送达、结算、来源身份或同事件幂等。

随包 Skill 原位发现，只有说明，无脚本或配置复制。安全仍由固定 `ContentSafetyPolicy` 执行，Skill 不是权限边界。最低插件版本声明不变，随包 Skill 能力需框架支持（本地核对 v4.24.1 起），真实旧版端到端仍未验证。

## 签到指令入口

签到业务使用 `/签到`、`/签到帮助`、`/签到状态`、`/签到成就`、`/签到日历`、`/刷新背景` 顶层入口，配合 `签到生日`、`签到称号`、`签到排行`、`签到商店`、`签到主题`、`签到管理` 六个指令组；纯文本“签到”继续由正则触发。各指令组的根路径由 AstrBot 显示对应指令树；`/签到帮助` 是独立普通指令，仅发送完整帮助图。旧 `签到中心` 路由和平铺业务指令不再保留。指令装饰器集中在 `main.py`，处理函数继续委托给 `CheckinCommandMixin` 与 `CheckinShopMixin`，避免命令结构与业务实现相互耦合。

## 签到商店扩展

`checkin/shop.py` 中的 `build_checkin_shop_items()` 是商品展示目录的统一注册点，每个商品拥有稳定 `item_id`、分类、指令、名称和价格。新增商品时先在目录中注册展示项，再在 `CheckinShopMixin` 增加购买处理，并将需要原子扣款的数据操作放入 `CheckinStore` 对应 store 模块；入口层只保留 AstrBot 指令装饰器。商品目录和购买行为应分别补充测试。

万象画卷生图额度（`item_id="omnidraw:quota"`）是首个条件商品：仅在总开关 `checkin_omnidraw_link_enabled` 开启且 `plugin._omnidraw_bridge`（`checkin/omnidraw_bridge.py`，initialize 中按开关创建）可用、快照 `available` 时注入目录；购买流程先经 `spend_coins` 扣款，再调用 bridge `grant` 发放，发放失败时用 `add_coins` 原路退回。

## 签到卡主题模板

每个主题位于 `templates/checkin_themes/<theme_id>/`，并提供 `style.css` 和 `preview.png`；默认、蓝、红、黄主题仍各自提供完整的 `index.html`。四季主题共用 `_shared/index.html` 和 `_shared/layout.css`，主题目录只保留配色样式与 `artwork.svg`，`get_checkin_card_template()` 在运行时将共享壳、主题 SVG 和 CSS 拼成最终自包含 HTML。最终模板会把 CSS 内联进 HTML 的 `/*__CHECKIN_CARD_CSS__*/` 标记，并将字体 base64 填入 `__CHECKIN_CARD_FONT_DATA__`，因此输出不得引用任何外部 URL 或跨目录资源；`test_all_registered_checkin_themes_are_self_contained` 会校验这一点。主题注册在 `checkin/themes.py`，模板内容变化时应同步 `version`，它参与签到卡缓存 key；共享四季壳或布局变化时，需要同步检查四季主题版本和缓存影响。

四季系列（`spring`/`summer`/`autumn`/`winter`）共用同一套布局 class（`.season-card`、`.season-info`）和信息区块，只有调色板、季节文案和装饰 SVG 不同。正式主题目录只保留共享壳所需的 `style.css`、`artwork.svg` 和最终 `preview.png`。开发期的构建脚本、预览渲染工具和测试素材只在本地工作区使用，不参与插件运行时和正式提交；改动装饰参数或预览素材后，应在本地重新生成对应正式目录中的 `preview.png`。

## 前端页面

`pages/pluginCenter/` 使用原生 HTML、CSS 和 ES module，提供群排行、成员当前数值编辑、内容安全、安全分组、数据管理和插件配置六个视图；顶部为紧凑品牌/安全摘要/刷新与单行下划线页签，主内容 1200px。明暗主题由宿主 `data-theme` 跟随，独立预览回退系统偏好，不读写浏览器存储；`ui.mjs` 提供自绘下拉与 checkbox＋独立轨道开关，配置页复用它们渲染枚举、模型和多选。会话策略由配置文件持久化；签到业务数据仍由 SQLite 和签到备份管理。成员编辑只更新 `checkin_users`，不回写 `checkin_records` 或 `checkin_group_presence`。

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
node --check pages/pluginCenter/settings.mjs
node --check pages/pluginCenter/ui.mjs
node --test tests/js/safety.test.mjs tests/js/settings.test.mjs tests/js/ui.test.mjs
python -m pytest -q tests
```
## 会话内容安全策略

`group_safety.py::SessionSafetyService` 从 `content_dedupe` 解析全局词、全局匹配模式、全局作品 ID 与命名分组。分组关联完整 UMO，单会话单组；未分组 R18 关闭。全局与分组关键词叠加，全局 ID 不可被分组覆盖。没有内置词表、局部作品黑名单或批量复制逻辑。

`main.py.safety_service` 在归档与配置保存完成后才允许 API 注册。旧配置通过隐藏 list 字段保留到初始化；服务原子写入内容哈希归档并核对，失败停止加载。旧数据库策略仅由 `checkin/group_safety_store.py` 读取归档；新安装不建旧策略／词表，旧表保留历史，不参与过滤。全局人工规则合并到新配置后，版本 1 禁止重读旧来源。

每次搜索、签到背景、日历背景或已保存背景恢复只解析一次不可变 `ContentSafetyPolicy`，传给取源、过滤与最终复核。规则服务缺失或校验失败拒绝取图；签到可按现有错误路径回退文字，不读取旧库当备用规则。

签到与日历缓存统一使用实际分级、匹配模式、规范化关键词和作品 ID 作为安全身份，不包含 UMO。相同实际规则可复用，不同规则不复用。关键词／ID 固定在本次快照，管理端并发更新不会改变进行中请求的过滤或缓存身份。

签到仍按用户和日期保存唯一奖励、内容与基础背景；重复签到恢复失败后的重选只更新规则变体缓存，不覆盖基础记录。背景摘要包含模式、来源、作品 ID、标题、作者和画质，不保存原图路径或完整元信息。摘要和 JPEG 同日期过期，缓存命中不重新取源。

Pages 后端规则写入配置；`image_index.sqlite3` 仅为黑名单保存辅助元信息／缩略图。辅助信息保存失败不否认配置已生效。`data-export` 导出签到、当前安全规则、辅助元信息与旧档案；`checkin-import` 只恢复签到，保持原同锁回滚保护。签到命令导出维持签到专用格式。

## 发图送达状态

`pixiv/delivery.py` 的 `send_once()` 返回五种状态：消息构建失败为 `FAILED`；调用正常返回为 `CONFIRMED`，但 OneBot file 正常返回且无有效 `message_id`（非零整数或非空白字符串，排除 bool／对象）时为 `ACCEPTED`（已受理，需保留文件）；发送异常链包含超时为 `TIMED_OUT`；其余提交异常为 `UNKNOWN`。超时按 `TimeoutError` 类型或 message/wording 中独立的 timeout/timed out/超时文本判断，先排除路径、URL 和无关回包字段，并保留路径字段后的独立超时诊断（含冒号分隔、无扩展名路径与“上传／读取超时”，不把 timeout.jpg 等文件名当诊断）；文件读取错误也只检查相同诊断文本，不因路径中的 copyfile 重发。包括 OneBot 1200 回包中的 `Timeout: NTEvent … sendMsg`，不能仅凭 1200 判定超时。

`search.py` 对 `CONFIRMED`、`ACCEPTED` 与 `TIMED_OUT` 均记录 normal 使用记录并结算金币；超时不补发，即使最终没有出图也扣费。`FAILED` 与构建期间取消释放未提交图片占用；`UNKNOWN` 与提交期间取消保留 normal_pending 占用，不扣费。OneBot `ACCEPTED`、超时、未知及提交期间取消保留文件读取窗口。构建和发送异常只记日志，不补发异常提示；正常金币结算消息保留。

异常日志只提取 `error_type`、`retcode`、`status`、脱敏 `message/wording`，并包含 `send_mode`、可用的作品 ID、`phase`、`delivery_status` 和 `duration_ms`。错误文本先截取前 2048 字符再脱敏，输出最多 512 字符，限制异常回包的正则处理开销与日志体积；地址、本地路径及 Windows UNC 共享路径、凭据和长数字标识会被遮蔽，不输出完整请求与回包。路径按盘符、UNC、file URI、位于词边界的根目录或点目录起点，以及带文件扩展名的相对路径识别；`NodeIKernelMsgService/sendMsg` 等服务名/方法名保留，不单凭标识内部的斜杠判定路径。file/path/filename 字段支持无引号的空格路径，以后续字段或分隔符为界；引号路径按对应引号闭合，兼容转义斜杠和相对路径后的方括号。编码切换、使用记录、去重占位释放失败分别记录，不重发已确认图片。

## OneBot 图片发送

`send_once()` 在 aiocqhttp 且事件存在 bot 时直接调用已有客户端的 call_action()，不另建连接。单图调用 send_group_msg/send_private_msg，合并转发调用 send_group_forward_msg/send_private_forward_msg，保留 self_id 路由。默认 file URI；构建或提交明确报文件不存在、权限不足、ENOENT/copyfile 等读取问题时，记录本次异常和 file → base64 切换，再重建同一消息尝试一次。第二次失败也独立记录；发送超时优先分类，绝不触发换编码。其他平台继续使用框架链路。

合并转发每图一个节点，节点内保留标题／ID与图片，缺图提示单独放在末尾；所有节点一次提交为一条转发。直发与合并共用原始 OneBot 自动传输策略，不区分可选发送链路。Tool 的 `send_mode` 只决定本次是否合并，不修改费用、去重或未知送达不补发规则。

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

`pixiv/search.py` 在所选画质下下载候选，超出大小上限、地址缺失或下载失败就换作品，先使用同批剩余候选，再按图片源优先级补取。首批全被漫画、安全规则或去重过滤时也继续补取。每次请求最多取 3 轮候选（每轮按优先级最多请求两个来源）、实际尝试下载 20 张作品，相同原始 ID 不重复下载；不足时发送已下载的图片。安全规则或去重索引异常停止选图；取消时等待正在执行的占用写入，再释放未提交占用。每张候选保留自己的 source_key，补取回退另一源后仍按对应来源写成功记录或释放 pending；跳过候选立即释放，未提交与取消仍由 finally 收尾。

`ImageDownloader.download_for_send()` 的 `allow_quality_fallback=False` 限定普通发图只尝试所选画质的反代和源地址；超软上限时清理文件并抛错交回选图层，50 MiB 硬上限不变。签到及日历传入 `size_limit_bytes=0` 并保留默认画质回退。持久配置沿用 `auto_downgrade_original_mb` 的键与数值，WebUI 名称改为图片大小上限。

首轮取源完成后以 `time.monotonic()` 建立 `request_timeout * max(3, count)` 秒的共享截止时间，单张下载（含反代轮换）及补取用 `asyncio.wait_for` 传入剩余预算。到期取消进行中的操作，发送已下载图片；首次取源、消息发送及清理不在预算内。外部取消继续传播。

Lolicon 无分页，每轮重新随机请求且不推进 offset。Pixiv 客户端返回 `PixivPage(items, next_params)`，`SourceRequest` 在请求内按 source_key 保存 continuation、重复位置集合和失败原因；列表为空但仍有下一页时保留状态并允许另一源回退。候选耗尽时保存下一页数字 offset，不依赖条数推算；无下一页则本次流结束。完整推荐参数只在请求内保存，跨请求继续使用现有数字起点，不变更数据库 schema。签到／预览复用该机制，日历保持单批；签到标签搜索显式传递 `portrait=True`，经已有认证请求入口启用竖图粗筛并进行本地精筛；不再由 Lolicon 比例字符串推断。推荐 continuation 的 `viewed` 与 `bookmark_illust_ids` 列表经校验、聚合后透传 SDK，重复位置比较使用不可变列表内容。旧的按候选数量推算分页方法已移除，索引只保存上游数字位置。

两源在响应边界校验必要列表、候选身份和分级等字段，使用 `SourceResponseError.reason` 暴露脱敏错误类别。Pixiv 的一次轻量重试只针对网络／超时错误，不因畸形响应或鉴权失败重试；不新增 `Retry-After` 逻辑。`SelectionIndexError` 区分本地索引故障和正常候选不足，不允许索引失效时继续放行。

大小约束由 `download()` 统一执行：Content-Length 超限时不读取正文，流式字节累计超限立即中止并清理半文件；`download_for_send()` 不重复检查返回大小。`ImageSizeLimitError` 按类型记录为 `size_limit` 并交回选图层，异常文案不参与分类。
