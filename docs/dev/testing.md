# 测试与回归检查

## 测试原则

只保留值得长期维护的功能与边界测试；短期诊断、历史修复过程和文案快照不进入长期测试集。测试按领域组织，优先复用现有场景覆盖行为、数据安全、单位切换和异常分类。

- UI布局、CSS取值、类名、固定帮助图高度与中文提示由人工／浏览器检查，不通过源码字符串快照锁定。
- 前端长期测试保留打包资源、相对桥接路径、草稿深拷贝和 UMO 单组边界；页面读写与错误交互由浏览器检查，不保留旧作用域批量复制状态机或布局快照。
- 模板保留自包含、资源可解码性和渲染画布边界，不绑定字体名字或页面区域类名。
- 当前仍支持的旧数据库迁移、备份及回滚是长期数据安全边界，不能因为名称包含旧版本就删除。
- 共用场景能覆盖同一功能时合并，独立调用路径、失败时点与安全边界不因相似命名删掉。

## 基础命令

| 场景 | 工作目录 | 命令 |
| --- | --- | --- |
| Python 语法检查 | 插件目录 | `python -m compileall -q main.py group_safety.py checkin pixiv tools plugin_api scripts/ci tests` |
| JSON schema 检查 | 插件目录 | `python -m json.tool _conf_schema.json` |
| JavaScript 语法检查 | 插件目录 | `node --check pages/pluginCenter/app.js`（含 `settings.mjs`、`safety.mjs`、`ui.mjs`） |
| 管理页状态测试 | 插件目录 | `node --test tests/js/safety.test.mjs tests/js/settings.test.mjs tests/js/ui.test.mjs` |
| 管理页真实浏览器回归（可选） | 插件目录 | `python -m pytest tests/test_plugin_center_browser.py`（需本机安装 Playwright 与 Chromium，否则整组跳过） |
| pytest 全部测试 | 插件目录 | `python -m pytest -v tests` |
| pytest 快速测试 | 插件目录 | `python -m pytest -q tests` |
| pytest 特定模块 | 插件目录 | `python -m pytest tests/test_checkin_*.py -v` |

> [!TIP]
> 这些是测试与检查命令，不是插件的独立运行命令。实际集成验证入口见 [`setup.md`](./setup.md#本地集成验证)。

本地全量测试显式指定 `tests`，避免收集被 Git 忽略的 `REFERENCE/` 等参考工程。质量门禁脚本也显式指定 `tests`，不能把参考插件的导入错误当成本插件测试失败。

## PR 快速 CI

PR 快速 CI（`AstrBot Plugin Quality Gate`，`.github/workflows/plugin-quality-gate.yml`）在 Ubuntu + Python 3.12 下安装最新正式版 AstrBot，通过 `scripts/ci/check_plugin_quality_gate.py` 执行 Python 编译检查、JSON schema 检查、前端 JavaScript/ES module 语法、分组草稿 JS 测试和全量 `python -m pytest -v tests`；同一个检查脚本也可以在本地执行。门禁会枚举 `pages/pluginCenter` 下全部 `.js`／`.mjs` 模块和 `tests/js/*.test.mjs`，新增文件无需再改门禁；同时从 `index.html` 递归解析相对资源：HTML 的 `src`／`href`、CSS 的 `url()`／`@import`、模块的静态／动态／副作用 `import`（单双引号均可），忽略 query 与 hash，任一层缺失或逃出页面目录即失败。`tests/test_plugin_center_resources.py` 直接复用门禁的 `resource_closure()`，只校验资源与桥接端点契约，不锁定文案、类名或页签顺序。生命周期检查脚本只服务于独立的 `AstrBot Plugin Lifecycle` CI。

本地等价质量门禁（在已安装项目依赖、`pytest`、`pytest-asyncio` 和 Node.js 的环境中执行）：

```bash
python scripts/ci/check_plugin_quality_gate.py
```

## PR 生命周期 CI

PR 生命周期 CI（`AstrBot Plugin Lifecycle`，`.github/workflows/plugin-lifecycle.yml`）不运行全量 pytest，也不承担基础静态检查；它只安装官方 AstrBot 和插件依赖，然后通过官方公开 `PluginManager.load()`、`reload()` 和 `uninstall_plugin()` 走完 `load → initialize → terminate → unbind` 路径。harness 只观察本轮新增的 handler、Web API、原生工具和后台任务并确认最终清理，不规定插件必须注册哪一类资源。

本地等价生命周期检查需要先准备 AstrBot 源码目录：

```bash
ASTRBOT_SOURCE="/path/to/AstrBot"
ASTRBOT_VERSION="$(git -C "$ASTRBOT_SOURCE" describe --tags --always)"
python scripts/ci/check_plugin_lifecycle.py \
  --astrbot-source "$ASTRBOT_SOURCE" \
  --astrbot-version "$ASTRBOT_VERSION" \
  --plugin-dir . \
  --astrbot-root /tmp/get-px-astrbot-root \
  --plugin-name astrbot_plugin_get_px
```

## 分层验证矩阵

| 改动类型 | 最小检查 | 建议额外回归 | 关注点 |
| --- | --- | --- | --- |
| Python 业务逻辑 | 语法检查、相关测试 | 命令路径手工验证 | 不要只跑被改函数附近的测试。 |
| 签到卡片渲染 | 语法检查、相关测试 | `/签到` 手工生成卡片 | 关注 T2I、主题、背景选择。 |
| 配置 schema | 语法检查、JSON 格式检查 | README 与配置文档同步核对 | 旧配置是否还能被容忍。 |
| 发图逻辑 | 语法检查、相关测试 | `/p 标签 2`、`/p 2`；开启自动触发后发送「来张图」 | 固定画质、大小筛选、换图补足、去重、过滤。 |
| 下载器 | 语法检查、下载器测试 | 实际下载验证 | 同画质反代轮换、大小边界、背景画质回退。 |
| 前端页面 | JavaScript 语法检查 | 打开 Plugin Pages 验证 | API 调用、数据展示、操作反馈。 |

## 高风险改动清单

| 改动 | 风险 | 建议 |
| --- | --- | --- |
| 下载器返回值 | 调用方解包失败 | 更新所有调用方并补充测试。 |
| 签到奖励规则 | 用户数据异常 | 补充回归测试覆盖边界情况。 |
| 去重索引逻辑 | 重复发图或漏图 | 覆盖跨天、清理和并发场景。 |
| 配置字段类型 | 旧配置加载失败 | 提供兼容方案或迁移脚本。 |
| SQLite schema | 数据丢失或损坏 | 提供迁移逻辑和回滚备份。 |
| 内容安全过滤 | 误杀或漏过 | 补充测试用例和日志观测。 |
| Lolicon 反代 | 下载失败或超时 | 覆盖全部失败、部分失败场景。 |

## 原生工具验证

`python -m pytest -q tests/test_image_tools.py tests/test_p_coin_cost.py` 验证共享消费边界，不锁 Skill 台词。真实本机执行器测试含工具超时、提交中取消、重复取消时等待事务结算；网络与 QQ 消息发送在测试侧替代，不能宣称真实图片送达。

Tool 领域测试覆盖 48 字符标签边界、短名称原样透传、非法发送方式在消费前拒绝、默认／省略single及显式auto/single/forward 到实际 OneBot action 的路由、非 OneBot 不合并、配置不变，以及换发送方式的重复调用不重发；结算和未知送达仍按共享规则。

`python -m pytest -q tests/test_image_selection_constraints.py tests/test_image_tools.py tests/test_p_coin_cost.py tests/test_image_selection.py tests/test_downloader.py` 验证请求级筛选：方向与比例／像素叠加、精确比例及绝对容差边界、非法参数消费前拒绝、管理员大小上限不能被放宽、配置0与请求省略、筛选发生在claim前、实际文件尺寸不符释放占用／清理／换图、有限补取及部分数量、同事件换条件不再消费，以及安全服务故障拒绝。真实框架执行器接受完整参数并仍只返回文本；图源和QQ由测试替身替代，不代表线上取图验证。

上线前另验证当前人格／会话的工具允许列表、插件停用／重载、Skill 发现与读取、模型一次调用完整数量、角色名翻译不擅自扩写、默认直发、仅用户明确要求合并时选择forward，不因数量多而自行合并、正常不报账单、余额缩减及未知送达不重试。随包 Skill 本地核对 v4.24.1 起支持；当前 `>=4.16` 只保留插件基础兼容声明，真实旧版本整链尚未测试。框架工具结果预览或自定义人格提示可能展示数值，插件不修改这些全局设置。

## T2I 回归

签到卡片渲染依赖 AstrBot 的 HTML/T2I 能力。修改签到卡片模板、样式或资源时，建议：

1. 运行语法检查和相关测试
2. 启动 AstrBot 并实际执行 `/签到`
3. 检查卡片布局、主题、背景和字体是否正常
4. 验证不同质量档位（省流量、清晰、极致）

渲染链路、各资产画布约定与自建端点本地重渲方法见 [`t2i-rendering.md`](./t2i-rendering.md)。

背景编码回归运行 `python -m pytest -q tests/test_checkin_card.py tests/test_checkin_calendar.py`，覆盖输出 1.5MB 预算、透明色重采样、透明 WebP、ICC 到 sRGB、无效 ICC 回退、40MP 解码前拦截和真实日历调用的 2880px 上限。上线前另用 `/签到`、`/签到日历` 验证三档真实 T2I 输出；本地编码测试不代表远端渲染成功。

头像与资源复用回归包含：QQ 头像内联、关闭头像及非 QQ 平台不下载、超时和无效响应使用昵称占位、取消时清理下载文件、三档仅准备一次资源、缓存命中不准备资源，以及所有主题注入动态数据后的外链检查。

跨群签到缓存回归运行 `python -m pytest -q tests/test_main_error_handling.py tests/test_checkin_cache.py`：使用真实 SQLite 和 JPEG 缓存验证 A→B→A 的相同/不同安全规则、仅 Lolicon 无 Pixiv token、缓存重载与并发、全局词和黑名单更新、背景刷新及主题切换、摘要/JPEG 缺失或损坏、清理重建不重复发奖，以及摘要原子写入失败不覆盖已有文件。测试替代远端取图和 T2I，不代表 QQ 实机发送验证。

## 测试覆盖建议

当前测试覆盖主要业务流程和边界情况：

### 签到测试

- `test_checkin.py` - 签到存储、奖励、连签与加持
- `test_main_error_handling.py` - 签到流程、跨群缓存、失败／超时与占用处理
- `test_checkin_feature_commands.py` - 商店、主题、生日和刷新入口
- `test_checkin_cache.py` - JPEG／摘要缓存、并发、校验与过期
- `test_checkin_background_selection.py` - 背景选择、预览避重与预占用
- `test_checkin_ranking.py` - 群排行和趋势

### 发图测试

- `test_lolicon.py`、`test_pixiv_client.py` - 图源规范化、固定 API 地址、优先级与双向失败／空结果回退；覆盖无 Pixiv 客户端、分页游标和安全参数传递
- `test_p_command_count.py` - 发图参数和数量
- `test_p_coin_cost.py`、`test_delivery.py` - 发送、结算、去重状态和诊断
- `test_downloader.py` - 图片下载、大小上限、固定画质与背景回退
- `test_image_index.py` - 去重索引、并发占用和黑名单
- `test_filtering.py` - 内容安全过滤
- `test_image_tools.py` - 原生 Tool 严格参数、完整 UMO、只读不建档、余额缩减／故障拒绝、静默结算、同事件重复保护、取消与卸载；真实本机框架执行器验证仅文本返回且不重发

### API 测试

- `test_plugin_management_api.py` - Plugin Pages 后端 API

## 回归场景

修改以下部分时，建议运行对应回归场景：

### 下载器改动

```bash
python -m pytest tests/test_downloader.py -v
# 手工验证：实际下载一张图片
# /p 1
```

### 签到改动

```bash
python -m pytest tests/test_checkin_*.py -v
# 手工验证：
# /签到
# /签到日历
# /签到商店
# /签到排行
```

### 配置改动

```bash
python -m json.tool _conf_schema.json
python -m pytest tests/test_command_registration.py -v
# 手工验证：检查新配置是否生效
```

### 前端改动

```bash
node --check pages/pluginCenter/app.js
node --check pages/pluginCenter/ui.mjs
node --test tests/js/safety.test.mjs tests/js/settings.test.mjs tests/js/ui.test.mjs
python -m pytest tests/test_plugin_center_browser.py
# 手工验证：页面加载、导航、草稿确认、读写、重置与移动端
# 浏览器检查明暗主题、配置搜索/切组草稿、数字清空阻止保存与键盘操作下拉
```

## 快速全面检查

提交代码前，建议运行以下全面检查：

```bash
# 1. 语法检查
python -m compileall -q main.py checkin pixiv plugin_api scripts/ci tests
python -m json.tool _conf_schema.json > /dev/null
node --check pages/pluginCenter/app.js
node --check pages/pluginCenter/settings.mjs
node --check pages/pluginCenter/ui.mjs

# 2. 只运行本插件的全部测试
python -m pytest -v tests

# 3. 检查是否有遗漏的文档更新
# 手工核对 README.md、docs/project/configuration.md 和 _conf_schema.json
```

## 性能回归

对于可能影响性能的改动（数据库查询、图片处理、大量并发），建议：

1. 使用 `time` 或 `pytest --durations=10` 测量执行时间
2. 对比改动前后的性能差异
3. 在日志中记录关键操作的耗时

## 常见测试问题

### 测试数据库冲突

如果测试使用临时数据库但发生冲突：

```python
# 确保测试使用 tempfile.TemporaryDirectory()
with tempfile.TemporaryDirectory() as tmp:
    # 测试逻辑
```

### Mock 对象签名不匹配

修改函数签名后，记得同步更新测试 mock：

```python
# 错误：mock 返回单个值
class FakeDownloader:
    async def download(self, url: str, timeout: float) -> str:
        return "/tmp/image.jpg"

# 正确：mock 返回元组
class FakeDownloader:
    async def download(self, url: str, timeout: float) -> tuple[str, int]:
        return "/tmp/image.jpg", 1024
```

### 异步测试

所有 async 函数的测试必须标记 `@pytest.mark.asyncio`：

```python
@pytest.mark.asyncio
async def test_something_async():
    result = await async_function()
    assert result is not None
```

## 发送状态与异常日志回归

运行 `python -m pytest -q tests/test_delivery.py tests/test_p_coin_cost.py`，验证 `CONFIRMED`、`ACCEPTED`、`TIMED_OUT`、`UNKNOWN`、`FAILED` 五种状态的结算与占用、取消、错误脱敏，以及群聊／私聊、单图／合并转发的消息构造。OneBot 自动采用 file，明确读取失败才重建为 base64；非 OneBot 使用框架发送，不再手动选择三种模式。

真实部署检查 file URI 的路径可见性，记录 `transport`、耗时和脱敏回包；在隔离环境模拟明确路径读取失败，确认 base64 重试一次；遇到发送超时确认没有补发。模拟测试不等于 QQ 实际送达验证。

## 缓存复用与去重回归

机制与身份见 [缓存复用、图片去重与幂等](../project/cache-and-dedupe.md)，本节只维护测试入口。

| 范围 | 测试文件 | 关注点 |
| --- | --- | --- |
| 图片使用索引 | `tests/test_image_index.py` | 原子占用、释放不删成功记录、自然日清理、分页游标与黑名单持久化 |
| 选图与预览 | `tests/test_checkin_background_selection.py` | 已用候选过滤、分页、5 分钟预览列表、下载取消释放 |
| 卡片缓存 | `tests/test_checkin_cache.py`、`tests/test_main_error_handling.py` | JPEG／摘要校验、实例内并发、跨群有效规则、恢复／重选、刷新／主题变化与不确定提交保留 |
| 日历与单次资源 | `tests/test_checkin_calendar.py`、`tests/test_checkin_card.py` | 记录／事件／画质 key、背景获取、头像内联与三档资源复用 |
| 外部资料 | `tests/test_holiday_calendar.py`、`tests/test_checkin_greeting.py`、`tests/test_checkin_omnidraw_bridge.py` | 初始化刷新条件、失败重试窗口、问候回退与额度幂等 |
| Pages 清理 | `tests/test_plugin_api_cache.py` | 不触碰数据库、跳过受保护下载文件、只清理受管缓存 |

跨会话／标签允许重复、图源 ID 表示差异曾用临时模拟核对，不是上述持久测试已经完整覆盖的承诺。以后修改去重范围须补专门用例，同时核对查询、原子占用、成功记录、释放与旧行兼容；不要只改候选排序。

```bash
python -m pytest -q tests/test_checkin_cache.py tests/test_checkin_calendar.py tests/test_checkin_card.py tests/test_checkin_background_selection.py tests/test_image_index.py tests/test_main_error_handling.py tests/test_holiday_calendar.py tests/test_checkin_greeting.py tests/test_plugin_api_cache.py tests/test_checkin_omnidraw_bridge.py
```

## 插件下载临时目录回归

`python -m pytest -q tests/test_downloader.py tests/test_plugin_api_cache.py` 验证指定目录落盘、未初始化不回退系统 tmp、错误时清理半成品，以及 Pages 不调用系统临时目录、只删除过期下载、保留活跃文件与无关文件。部署检查需确认日志中的 file URI 对协议端可见，共享 data 目录不等于两端绝对路径自动相同。

下载器测试另覆盖未知路径发送保留 600 秒后自动删除、mtime 更新、关闭任务但保留文件、重启按剩余保护时间恢复清理及忽略无关文件。签到流程测试验证缓存查询抛 OSError 时仍返回文字、释放占用并清理原图，不产生 UnboundLocalError。

### 跨用户图片读取回归

`tests/test_delivery.py` 覆盖 POSIX file 序列化设置 `0644`、Windows 与 base64 不改权限、合并转发嵌套图片，以及 chmod 权限失败记录构建错误后切换 base64。真实 mkstemp `0600→0644` 断言仅在 POSIX 执行，Windows 明确跳过；跨容器不同 UID 的读取仍需部署端验证。

## 发布信息回归

运行 `python -m pytest -q tests/test_release_metadata.py`，核对 metadata、运行时常量、README 徽章、CHANGELOG 首个正式版本及日期一致，更新日志链接指向对应版本，文档措辞由审查核对，不保留禁止词快照。首个版本段仍要求正式版本，避免误读取历史版本。发布触发与风险见 [发布前检查](contributing.md#发布前检查)。

## 配置升级回归

运行 `python -m pytest -q tests/test_config_upgrade.py tests/test_group_safety_config.py tests/test_checkin_group_safety.py`。`tests/fixtures/config_v3_8_0_grouped.json` 是按 v3.8.0 分组格式构造的合成配置，不含用户数据。使用已安装 AstrBot 的真实 `AstrBotConfig` 加载、裁剪、保存、重载，核对六组自定义值、首选图源默认与保存重载、旧 Lolicon 地址裁剪、去重天数、隐藏旧列表原样归档、全局人工规则保留、完整 UMO 单组归属、保存／归档失败不清配置，确认旧词在版本 1 重载时不会复活，以及新安装默认值正常。

这些用例不调用 v3.8.0 的旧迁移代码，不证明更早版本可直接升级；它们验证已完成分组迁移的配置与当前 schema 的兼容性。本机验证框架为 AstrBot 4.27.3，不替代生产升级或其他框架版本验证。

设置页后端另由 `tests/test_plugin_settings_api.py` 覆盖：真实 `AstrBotConfig` 保存与重载、安全四键与隐藏键拒绝写入、令牌只输出是否配置且空文本不清除、独立清除字段、类型／范围／选项校验、provider 枚举失败保留已存 ID，以及保存失败时嵌套容器与列表引用回滚。`tests/js/settings.test.mjs` 验证前端纯函数契约：选项规范化（含 provider 空选项与未知旧值）、部分补丁与清除令牌分离，以及多选不会把未声明值扩成可选项。

## OneBot 自动传输与超时结算

运行 `python -m pytest -q tests/test_delivery.py tests/test_p_coin_cost.py tests/test_downloader.py`。覆盖群聊/私聊、单图/转发、空回执正常结算并保留 file 图片、路径含 timeout 不误判、1200 NTEvent 超时仍扣费且去重、不发异常提示、不重发、文件读取失败换 base64 的两次诊断日志、超时文件保留 600 秒、非超时未知状态及取消处理，以及旧传输配置清理/保存失败回滚。测试使用模拟协议回包，不代表 QQ 实际延迟验证。

签到卡发送回归运行 `python -m pytest -q tests/test_main_error_handling.py tests/test_delivery.py tests/test_checkin_cache.py`：验证每日签到与付费刷新超时／送达未知不补文本，提交期间取消保留背景、卡片和占用，构建期取消释放未提交资源；覆盖重复签到复用、路径失败切 base64、无缓存刷新延迟清理，以及真实超时诊断和畸形回执分类。

日历缓存回归运行 `python -m pytest -q tests/test_checkin_calendar.py`，用实际事件快照验证备份导入编辑名称／日期、在线节假日更新均改变缓存 key，相同内容仍复用 JPEG；同一快照传给渲染，避免 key 与内容错配。

### 双图源补选与分页

运行 `python -m pytest -q tests/test_image_selection.py tests/test_pixiv_pagination.py tests/test_pixiv_client.py tests/test_lolicon.py tests/test_checkin_background_selection.py tests/test_p_coin_cost.py tests/test_image_index.py`。测试使用SDK／HTTP替身与临时SQLite，不访问真实图源或QQ。

- `test_image_selection.py`：过滤后补取、服务故障停止、空页跨源回退与续页、循环终止、末批位置保存，以及普通发图／签到占用写入途中取消后的清理。
- `test_pixiv_pagination.py`：推荐正数及0／-1边界、列表参数与SDK编码回放；非可信地址、非法数值／列表、畸形响应拒绝；空页、多页与缺失可选信息；异常信息不泄露。
- 既有客户端、Lolicon、签到背景测试：认证搜索、图源响应归一化、标签竖图路由、本地0.60–0.90精筛与背景占用失败。
- 既有索引测试：精确写入／覆盖／清零、scope／source隔离、重开持久化和TTL过期。

跨请求推荐continuation仍只有数字offset持久化，请求内测试不代表完整断点续读。

### 日志安全与状态

错误信息脱敏、空回执状态及安全故障降级随对应的分页、发送、安全过滤和生图桥接领域测试验证，不单独维护中文措辞或诊断类别快照。logger导入约定通过代码审查核对。大小格式的1MiB单位边界在下载器测试中验证；API和大小限制仍按字节计算。

日志中的`raw_candidate_count`表示过滤前作品数，“下载尝试”表示作品下载次数，而不是本批大小。续页诊断字段仅输出固定类别与已知字段名，不记录完整地址、未知参数名和参数值。

#### 真实图源验证记录（2026-11-07）

通过本地 HTTP 代理执行 9 项小范围实测：Pixiv refresh token 登录、推荐首批和下一页、标签搜索首批和下一页、竖图标签搜索、Lolicon 普通分级竖图取样，以及两源各一张 `large` 下载。全部通过，图片均经 Pillow 格式和完整性校验；凭据仅在内存中读取，未写入脚本或报告。

- Pixiv 推荐两批分别返回 67／69 条，下一页包含 offset 与收藏游标等参数，客户端成功解析并继续。
- Pixiv 标签搜索两批各 30 条；竖图搜索 30 条均宽小于高，其中 21 条符合本地 0.60–0.90 精筛，粗筛不能替代精筛。
- Lolicon 返回 2 条普通分级结果，宽高比分别约 0.620／0.707；本次 AI 标识均为未知，不作为 AI 排除有效性的证据。
- Pixiv 下载为 WEBP（600×761，140856 bytes），Lolicon 下载为 JPEG（744×1200，566799 bytes），均使用返回的 `large` 地址，未验证原图档位。

该记录只验证一次真实图源样本和当前 SDK；代理在测试进程显式注入，没有修改插件正式代理配置。未执行 QQ 发送、签到奖励、金币结算或 T2I；未测试自定义反代轮换、实际限流、异常回包与长期运行。Lolicon 文档域名的证书失败不代表取图 API 失败，本次 API 与默认返回图片地址均可通过代理访问。临时探针与脱敏报告仅存放于工作区外，不是仓库内可直接复现的测试脚本。

### 大小上限与候选补选

`tests/test_downloader.py` 覆盖原图／大图／中图超限后清理且不降档、等于阈值与 0 关闭软上限、同画质反代重试、缺失所选画质拒绝、背景下载保留回退。`tests/test_p_coin_cost.py` 使用真实下载器与 SQLite 索引验证超限后同批补足、淘汰图释放占用且不扣费，并覆盖跨来源补取成功／下载取消、重复候选不重复下载、3 批／20 张上限，以及补取失败仍发送并结算已有图片。网络与协议回包使用测试替身，不代表真实图源和 QQ 验证。

软上限回归验证声明长度超限不读取正文、未知长度超限后停止迭代并清理半文件；时间预算回归用短时测试替身验证下载与候选补取超时取消网络操作，发送并结算已有图片。

多图时间预算回归用模拟时钟验证 1／5／20 张及 5／30 秒超时配置不会被固定 90 秒截断；超限拒绝测试走真实下载器并验证清理及错误类型分类。

## 安全分组与管理页

- `tests/test_filtering.py`：宽松／严格、完整翻译标签、标题与简介边界、规范化、R18／关键词／PID 同快照生效、规则故障拒绝。
- `tests/test_group_safety_config.py`、`test_config_upgrade.py`、`test_checkin_group_safety.py`：完整 UMO 隔离、重复归属拒绝、真实 AstrBot 配置加载、旧库原始词保留、原子归档核对、失败不清与幂等、SQLite 备份／回滚和签到历史保留。
- `tests/test_main_error_handling.py`、`test_checkin_calendar.py`：实际规则变化使缓存失效、进行中快照不受配置并发变化影响；相同规则可复用。
- `tests/test_plugin_management_api.py`、`test_checkin_backup_web.py`：配置／Pages 同源、辅助信息不决定黑名单、数据包无凭据、导入只恢复签到、原回滚保护。
- `node --test tests/js/safety.test.mjs tests/js/settings.test.mjs tests/js/ui.test.mjs`：草稿深拷贝、R18 默认关闭、UMO 单组验证，设置页选项规范化、跨组搜索、数字清空/越界判定、多选旧值保留与部分补丁／清除令牌的纯函数契约，以及自绘下拉的选项规范化与键盘下标边界。旧状态机与专属测试已删除，不保留布局／台词／历史实现身份快照。
- 页面接线及视觉用浏览器验证：新建、编辑、保存失败、草稿关闭确认、重复 UMO、删除、模式／全局词、导出；桌面与移动端检查明暗主题、溢出、焦点和弹窗滚动；配置页检查跨组搜索草稿、改回无改动、放弃／重新载入确认、保存失败保留、数字清空阻止保存、自绘下拉的 ↑↓/Home/End/Enter/Space/Escape/Tab 与开关键盘焦点。离线 bridge 验证不等于真实 AstrBot 页面鉴权或 QQ 端到端。

### 管理中心浏览器回归（可选依赖）

`tests/test_plugin_center_browser.py` 使用本机 Python Playwright 驱动真实 Chromium，通过临时本地 HTTP 提供页面，并用合成 bridge 数据验证：保存期间输入框、开关、下拉、搜索、分组导航、手机组选择、清除令牌、放弃与重新载入全部冻结；成功与失败都恢复控件原有 disabled 状态（勾选清除令牌后密码框仍禁用，切组与搜索重渲后保持）；保存失败保留草稿；主页刷新不重载设置草稿；后台刷新不关闭安全分组弹窗或丢弃其中草稿；字段标签／开关名称与 `aria-describedby` 可被真实可访问名查询命中；无效数字同样触发放弃与重新载入确认，取消时保留草稿、确认后恢复已保存值；数字步长不合规时字段内提示并禁用保存；页签方向键／Home／End 自动激活与 roving tabindex；跳转链接把焦点移到主内容。模块在缺少 Playwright 时 `importorskip`，CI 不安装该可选依赖，因此本地通过不等于 CI 执行；测试不访问生产 AstrBot、QQ、Pixiv 或真实用户配置。
