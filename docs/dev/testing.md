# 测试与回归检查

## 基础命令

| 场景 | 工作目录 | 命令 |
| --- | --- | --- |
| Python 语法检查 | 插件目录 | `python -m compileall -q main.py checkin pixiv tools plugin_api scripts/ci tests` |
| JSON schema 检查 | 插件目录 | `python -m json.tool _conf_schema.json` |
| JavaScript 语法检查 | 插件目录 | `node --check pages/pluginCenter/app.js` |
| pytest 全部测试 | 插件目录 | `python -m pytest -v tests` |
| pytest 快速测试 | 插件目录 | `python -m pytest -q tests` |
| pytest 特定模块 | 插件目录 | `python -m pytest tests/test_checkin_*.py -v` |

> [!TIP]
> 这些是测试与检查命令，不是插件的独立运行命令。实际集成验证入口见 [`setup.md`](./setup.md#本地集成验证)。

本地全量测试显式指定 `tests`，避免收集被 Git 忽略的 `REFERENCE/` 等参考工程。质量门禁脚本也显式调用 `python -m pytest -v tests`；不能把参考插件的导入错误当成本插件测试失败。

## PR 快速 CI

PR 快速 CI（`AstrBot Plugin Quality Gate`，`.github/workflows/plugin-quality-gate.yml`）在 Ubuntu + Python 3.12 下安装最新正式版 AstrBot，通过 `scripts/ci/check_plugin_quality_gate.py` 执行 Python 编译检查、JSON schema 检查、前端 JavaScript 语法检查和全量 `python -m pytest -v tests`；同一个检查脚本也可以在本地执行。生命周期检查脚本只服务于独立的 `AstrBot Plugin Lifecycle` CI。

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
# 手工验证：打开 Plugin Pages 管理中心
```

## 快速全面检查

提交代码前，建议运行以下全面检查：

```bash
# 1. 语法检查
python -m compileall -q main.py checkin pixiv tools plugin_api scripts/ci tests
python -m json.tool _conf_schema.json > /dev/null
node --check pages/pluginCenter/app.js

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

运行 `python -m pytest -q tests/test_release_metadata.py`，核对 metadata、运行时常量、README 徽章、CHANGELOG 首个正式版本及日期一致，更新日志链接指向对应版本，用户和项目文档没有分支临时措辞。版本段前插入“待发布”标题会导致测试失败，不再跳过该标题继续匹配历史版本。发布触发与风险见 [发布前检查](contributing.md#发布前检查)。

## 配置升级回归

运行 `python -m pytest -q tests/test_config_upgrade.py tests/test_group_safety_config.py tests/test_checkin_group_safety.py`。`tests/fixtures/config_v3_8_0_grouped.json` 是按 v3.8.0 分组格式构造的合成配置，不含用户数据。使用已安装 AstrBot 的真实 `AstrBotConfig` 加载、裁剪、保存、重载，核对六组自定义值、首选图源默认与保存重载、旧 Lolicon 地址裁剪、去重天数、群／私聊独立列表与元数据保留，确认分组空列表不复活顶层旧策略，以及新安装默认值正常。

这些用例不调用 v3.8.0 的旧迁移代码，不证明更早版本可直接升级；它们验证已完成分组迁移的配置与当前 schema 的兼容性。本机验证框架为 AstrBot 4.27.3，不替代生产升级或其他框架版本验证。

## OneBot 自动传输与超时结算

运行 `python -m pytest -q tests/test_delivery.py tests/test_p_coin_cost.py tests/test_downloader.py`。覆盖群聊/私聊、单图/转发、空回执正常结算并保留 file 图片、路径含 timeout 不误判、1200 NTEvent 超时仍扣费且去重、不发异常提示、不重发、文件读取失败换 base64 的两次诊断日志、超时文件保留 600 秒、非超时未知状态及取消处理，以及旧传输配置清理/保存失败回滚。测试使用模拟协议回包，不代表 QQ 实际延迟验证。

签到卡发送回归运行 `python -m pytest -q tests/test_main_error_handling.py tests/test_delivery.py tests/test_checkin_cache.py`：验证每日签到与付费刷新超时／送达未知不补文本，提交期间取消保留背景、卡片和占用，构建期取消释放未提交资源；覆盖重复签到复用、路径失败切 base64、无缓存刷新延迟清理，以及真实超时诊断和畸形回执分类。

日历缓存回归运行 `python -m pytest -q tests/test_checkin_calendar.py`，用实际事件快照验证备份导入编辑名称／日期、在线节假日更新均改变缓存 key，相同内容仍复用 JPEG；同一快照传给渲染，避免 key 与内容错配。

### 大小上限与候选补选

`tests/test_downloader.py` 覆盖原图／大图／中图超限后清理且不降档、等于阈值与 0 关闭软上限、同画质反代重试、缺失所选画质拒绝、背景下载保留回退。`tests/test_p_coin_cost.py` 使用真实下载器与 SQLite 索引验证超限后同批补足、淘汰图释放占用且不扣费，并覆盖跨来源补取成功／下载取消、重复候选不重复下载、3 批／20 张上限，以及补取失败仍发送并结算已有图片。网络与协议回包使用测试替身，不代表真实图源和 QQ 验证。

软上限回归验证声明长度超限不读取正文、未知长度超限后停止迭代并清理半文件；时间预算回归用短时测试替身验证下载与候选补取超时取消网络操作，发送并结算已有图片。

多图时间预算回归用模拟时钟验证 1／5／20 张及 5／30 秒超时配置不会被固定 90 秒截断；超限拒绝测试走真实下载器并验证清理及错误类型分类。

## 模型取图验证

`python -m pytest -q tests/test_image_tools.py tests/test_image_selection_constraints.py tests/test_p_coin_cost.py tests/test_main_error_handling.py tests/test_plugin_lifecycle.py` 覆盖参数拒绝、现有安全策略、余额读取／缩减／免费路径、直发与合并、真实文件筛选、同事件幂等、送达未知、重复取消结算与工具卸载。生命周期 harness 通过官方管理器观察原生工具注册、重载和解绑，质量门禁包含 tools 编译检查。

真实聊天还需验证当前模型可见工具、随包 Skill 可发现以及 OneBot 实际收图；单元测试使用网络和发送桩，不替代协议端收图确认。

数量限制回归同时覆盖免费模式配置截断、配置与余额叠加、缩减提醒不被低余额提醒覆盖，以及候选不足不冒充配置限制。结算异常用实际已提交扣款模拟，确认返回 unknown、不泄露异常原文且不重发；比例筛选覆盖窄比例默认容差和显式精确匹配。

工具与命令的失败路径回归覆盖无候选、过滤为空、下载耗尽、索引失败和限频，验证不新增发送、扣费或重复取图；执行中重复调用仍返回已有状态。具体提示文案用临时一致性检查核对，不保留整段中文快照。

公开查询输入与固定SDK边界见 `tests/test_pixiv_queries.py`；扩展取图回归见 `tests/test_public_discovery.py`：覆盖四工具注册与卸载、无 token 随机取样、Pixiv 专属模式、免费无副作用、查询后发图、上游顺序、指定页与重取、候选缓冲、真实续查、游标隔离／失效／淘汰、扫描及超时预算。相关回归与完整 `python -m pytest -v tests` 均需通过；官方生命周期须确认四个工具重载与卸载无残留。离线桩不代表真实 Pixiv、模型选择或协议端收图通过。

扩展取图还覆盖关联种子与结果尺寸分离、Lolicon具体页码和未知总页数、非首张的实际文件尺寸校验，以及指定详情过滤状态。

`tests/test_filtering.py` 使用真实 SQLite 索引覆盖随机选图与上游顺序的独立开关、开启／关闭去重，以及整页已用时仅普通模式推进命令游标；明确 Pixiv 模式不得推进命令游标。
