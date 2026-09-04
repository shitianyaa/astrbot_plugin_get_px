# 测试与回归检查

## 基础命令

| 场景 | 工作目录 | 命令 |
| --- | --- | --- |
| Python 语法检查 | 插件目录 | `python -m compileall -q main.py checkin pixiv plugin_api scripts/ci tests` |
| JSON schema 检查 | 插件目录 | `python -m json.tool _conf_schema.json` |
| JavaScript 语法检查 | 插件目录 | `node --check pages/pluginCenter/app.js` |
| pytest 全部测试 | 插件目录 | `python -m pytest -v` |
| pytest 快速测试 | 插件目录 | `python -m pytest -q` |
| pytest 特定模块 | 插件目录 | `python -m pytest tests/test_checkin_*.py -v` |

> [!TIP]
> 这些是测试与检查命令，不是插件的独立运行命令。实际集成验证入口见 [`setup.md`](./setup.md#本地集成验证)。

## PR 快速 CI

PR 快速 CI（`AstrBot Plugin Quality Gate`，`.github/workflows/plugin-quality-gate.yml`）在 Ubuntu + Python 3.12 下安装最新正式版 AstrBot，通过 `scripts/ci/check_plugin_quality_gate.py` 执行 Python 编译检查、JSON schema 检查、前端 JavaScript 语法检查和全量 `python -m pytest -v`；同一个检查脚本也可以在本地执行。生命周期检查脚本只服务于独立的 `AstrBot Plugin Lifecycle` CI。

本地等价质量门禁（在已安装项目依赖、`pytest`、`pytest-asyncio` 和 Node.js 的环境中执行）：

```bash
python scripts/ci/check_plugin_quality_gate.py
```

## PR 生命周期 CI

PR 生命周期 CI（`AstrBot Plugin Lifecycle`，`.github/workflows/plugin-lifecycle.yml`）不运行全量 pytest，也不承担基础静态检查；它只安装官方 AstrBot 和插件依赖，然后通过官方公开 `PluginManager.load()`、`reload()` 和 `uninstall_plugin()` 走完 `load → initialize → terminate → unbind` 路径。harness 只观察本轮新增的 handler、Web API 和后台任务并确认最终清理，不规定插件必须注册哪一类资源。

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
| 发图逻辑 | 语法检查、相关测试 | `/来点图`、`/p`、`/pid` 手工验证 | 质量、去重、过滤、降级。 |
| 下载器 | 语法检查、下载器测试 | 实际下载验证 | 反代轮换、大小计算、降级。 |
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

## 测试覆盖建议

当前测试覆盖主要业务流程和边界情况：

### 签到测试

- `test_checkin_application.py` - 签到流程、奖励计算、连签
- `test_checkin_rules.py` - 金币和好感度规则
- `test_checkin_store.py` - 数据存储和查询
- `test_checkin_shop.py` - 商店购买和加持
- `test_checkin_ranking.py` - 群排行和趋势

### 发图测试

- `test_pixiv_search.py` - Lolicon 搜索和 Pixiv 回退
- `test_downloader.py` - 图片下载、反代轮换、质量降级
- `test_image_index.py` - 去重索引和黑名单
- `test_filters.py` - 内容安全过滤

### API 测试

- `test_plugin_management_api.py` - Plugin Pages 后端 API

## 回归场景

修改以下部分时，建议运行对应回归场景：

### 下载器改动

```bash
python -m pytest tests/test_downloader.py -v
# 手工验证：实际下载一张图片
# /来点图
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
python -m compileall -q main.py checkin pixiv plugin_api scripts/ci tests
python -m json.tool _conf_schema.json > /dev/null
node --check pages/pluginCenter/app.js

# 2. 运行全部测试
python -m pytest -v

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
