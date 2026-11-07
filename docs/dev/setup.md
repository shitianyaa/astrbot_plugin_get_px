# 开发环境与本地调试

## 前置要求

- Python 3.10+
- AstrBot 本地开发环境
- 推荐使用虚拟环境

## 可选依赖

签到卡片运行时依赖 AstrBot 的 HTML/T2I 渲染能力。开发和排查签到卡片问题时，建议准备一个本地 T2I 服务。

当前测试不强制要求本地 T2I 服务，但签到卡片视觉效果需要真实渲染验证。渲染链路、画布约定与自建端点本地重渲方法见 [`t2i-rendering.md`](./t2i-rendering.md)。

## 本地代码位置

插件通常位于 AstrBot 数据目录下的插件目录中，例如：

```text
AstrBot/data/plugins/astrbot_plugin_get_px
```

## 常用目录

```text
checkin/        # 签到业务：数据模型、规则、存储、流程、商店、卡片渲染
pixiv/          # Pixiv 作品获取：搜索、下载、过滤、客户端
plugin_api/     # Plugin Pages 后端 API
pages/          # Plugin Pages 前端页面（HTML/CSS/JS）
tests/          # 测试入口和回归用例
docs/           # 项目和开发文档
Progress/       # 开发任务记录和进度跟踪
```

## 运行与调试原则

### 本地集成验证

项目运行通常启动 AstrBot 顶层入口，而不是直接运行插件目录内的文件。

本地工作区常见入口：

```bash
# 从 AstrBot 根目录启动
python main.py
```

或使用虚拟环境：

```bash
source venv/bin/activate  # Linux/macOS
.\venv\Scripts\activate   # Windows
python main.py
```

### 数据目录

不要把运行时数据写回插件仓库下的目录。

本插件通过 AstrBot 提供的 `StarTools.get_data_dir("astrbot_plugin_get_px")` 取得插件数据目录。

签到数据、去重索引、黑名单和缓存都保存在插件数据目录中。

### 启动结构

入口和主要流程见 [`../project/architecture.md`](../project/architecture.md)。本地调试时不要把复杂业务逻辑塞回 `main.py`。

## 测试与检查命令

命令清单统一维护在 [`testing.md`](./testing.md)，本文件不重复列出。

## 开发工作流

1. 阅读相关文档（`docs/project/` 和 `docs/dev/`）
2. 理解当前架构和边界
3. 修改代码
4. 运行语法检查和相关测试
5. 手工验证相关功能路径
6. 同步受影响文档
7. 记录进度到 `Progress/` 目录
8. 提交代码

## 常见开发场景

### 修改发图逻辑

1. 阅读 [`../project/architecture.md`](../project/architecture.md) 的 `pixiv/` 部分
2. 修改相关模块（`search.py`、`downloader.py`、`filters.py` 等）
3. 运行相关测试：`python -m pytest -q tests/test_lolicon.py tests/test_pixiv_client.py tests/test_filtering.py tests/test_delivery.py tests/test_p_coin_cost.py tests/test_image_index.py`
4. 手工验证：`/p 关键词 2`、`/p 2`；开启自动触发后发送「来张图」

### 修改签到逻辑

1. 阅读 [`../user/checkin.md`](../user/checkin.md) 了解业务规则
2. 修改相关模块（`checkin/application.py`、`checkin/rules.py` 等）
3. 运行相关测试：`pytest tests/test_checkin_*.py -v`
4. 手工验证：`/签到`、`/签到状态`、`/签到商店`、`/签到主题`、`/签到日历`

### 修改配置项

1. 阅读 [`../project/configuration.md`](../project/configuration.md)
2. 修改 `_conf_schema.json`
3. 同步更新 `README.md` 和 `docs/project/configuration.md`
4. 修改代码中的配置读取逻辑
5. 运行全部测试确保兼容性

### 修改前端页面

1. 前端代码位于 `pages/pluginCenter/`
2. 使用原生 HTML、CSS 和 ES module
3. 修改后检查语法：`node --check pages/pluginCenter/app.js`
4. 手工验证：打开 Plugin Pages 管理中心

## 调试技巧

### 查看日志

插件日志会输出到 AstrBot 日志系统，日志前缀为 `[GetPx]`。

### 查看数据库

先从插件日志或 `StarTools.get_data_dir("astrbot_plugin_get_px")` 确认真实数据目录，再使用已存在的 `.sqlite3` 文件。不要猜测目录或文件名；SQLite 对不存在的路径默认可能新建空库。下列命令使用只读模式，不在源码目录执行建库：

```bash
sqlite3 -readonly "<插件数据目录>/checkin.sqlite3" ".tables"
sqlite3 -readonly "<插件数据目录>/checkin.sqlite3" ".schema checkin_records"
sqlite3 -readonly "<插件数据目录>/image_index.sqlite3" ".schema image_usage"
sqlite3 -readonly "<插件数据目录>/image_index.sqlite3" "SELECT date_key, scope, source_key, illust_id, feature FROM image_usage ORDER BY sent_at DESC LIMIT 10;"
```

去重查询按会话、图源／标签和原始图片 ID 隔离。JPEG 与摘要在 `checkin_card_cache/`，不在 SQLite；详细存储与清理边界见 [缓存与去重](../project/cache-and-dedupe.md)。不要把包含用户资料的查询结果直接贴入公开日志或 issue。

### 重置隔离测试数据

测试使用新建的临时数据目录和临时数据库，不操作真实 Bot 的数据目录。手工集成测试也应配置独立 AstrBot 数据目录；停止测试实例、确认备份及路径后，只清理本轮生成的隔离目录，再启动测试实例。

不要为排查重复图片删除生产 `checkin.sqlite3` 或 `image_index.sqlite3`。Pages 清理缓存不清空签到奖励、作品去重、黑名单或备份。
