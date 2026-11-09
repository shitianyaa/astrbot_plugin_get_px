# AGENTS.md - astrbot_plugin_get_px

本文件只保留协作 agent 的入口规则。项目细节按需阅读 `docs/project/`，开发维护规则优先阅读 `docs/dev/maintenance.md`。

## 沟通语言

- 与用户沟通必须使用中文。

## 项目形态

- **语言与框架**: Python 3.10+ / AstrBot plugin system
- **插件职责**: Pixiv 图片获取、每日签到系统、金币商店、群排行、内容安全过滤、Plugin Pages 管理界面
- **主要目录**:
  - `main.py`: 插件生命周期、指令装饰器、配置读取和领域对象装配
  - `checkin/`: 签到业务（数据模型、规则、存储、商店、卡片渲染）
  - `pixiv/`: 作品获取（搜索、下载、安全过滤、多日去重索引）
  - `tools/` & `skills/`: 原生模型取图入口、请求内幂等、随包使用说明
  - `plugin_api/` & `pages/`: Plugin Pages 后端 API 与前端管理界面（原生 HTML/CSS/JS）
  - `tests/` & `docs/`: 自动化测试、项目文档
  - `Progress/`: 本地开发进度记录（仅本地使用，不入库）

## 技能与检索指引

- **AstrBot 插件开发**: 涉及 AstrBot 插件生命周期、指令装饰器、消息链构造、事件流及配置 Schema 时，**必须参考 `skill-astrbot-dev`**。
- **代码检索**: 仓库建有 CodeGraph 索引（`.codegraph/`）时，代码定位优先使用 CodeGraph。

## 阅读入口

- 任何改动前先看：`docs/dev/maintenance.md`
- 架构与业务流转：`docs/project/architecture.md`
- 缓存、去重与幂等：`docs/project/cache-and-dedupe.md`
- 配置项核对：`_conf_schema.json`、`README.md`、`docs/project/configuration.md`
- 测试与工程规范：`docs/dev/testing.md`、`docs/dev/engineering-principles.md`

## 核心硬约束

- **入口职责**: 复杂业务逻辑放在领域模块，严禁将业务逻辑塞回 `main.py`。
- **数据路径**: 必须使用 `StarTools.get_data_dir("astrbot_plugin_get_px")`，严禁依赖/创建 `<plugin>/data`。
- **下载规范**: `downloader.download()` 为异步方法，需传 `url`、`timeout`，返回 `(path, size)` 元组，调用方必须显式解包。
- **配置读取**: 统一通过 `main.py` 封装方法读取，严禁散落调用 `self.context.get_config(...)`；schema 仅保留六个正式分组，配置升级基线为已完成迁移的 v3.8.0，兼容边界见 `docs/project/configuration.md`。
- **数据库迁移**: SQLite schema 变更必须在 `checkin/schema.py` 提供版本迁移与备份机制。
- **安全规则**: 全局词、单一匹配模式、全局作品 ID 与完整 UMO 命名分组统一存配置；无内置词，分组 R18 默认关闭。旧键只作备份桥接，核对归档后停用；不恢复旧策略，不回退旧库作有效规则。
- **模型取图**: 复用共享选图／发送／结算；只当前会话、正常账单不外发、实际状态文本返回。同事件只执行一次，不补发未知送达；停用先取消并等待在途结算／清理再关闭资源。Skill 不是安全边界。
- **主题自包含**: 签到卡主题生成必须内联 CSS/base64 字体，严禁外部网络 URL。
- **日志规范**: logger 统一通过 `from astrbot.api import logger` 导入，禁止使用 Python 内置 `logging` 或以其作为导入失败的回退；独立测试如需替代日志器，在测试侧注入 mock。日志使用“中文结果描述 + `snake_case=value` 字段”，区分调用返回、已受理、送达未知和已确认，不将待核对状态写成成功；禁止直接拼接凭据、完整响应或任意异常原文，禁止吞掉异常。

## 测试原则

- 只保留值得长期维护的功能、安全和数据边界测试，优先复用领域场景；短期诊断、历史修复过程和文案快照不进入长期测试集。

## 文档与进度纪律

- 文档是改动的一部分。指令、配置、流程、数据结构或规则变更时，必须在同一 patch 中更新相关 `docs/`、`README.md` 及 schema。
- repo-wide 约定变化时，同步更新 `AGENTS.md`（严控 100 行内）。
- 每次开发任务在 `Progress/YYYY-MM-DD[-XX].md` 记录：需求、实现清单、状态、改动文件、问题与决策（该目录仅本地使用，不提交）。

## 常用测试与检查

```bash
python -m json.tool _conf_schema.json
python -m compileall -q main.py group_safety.py checkin pixiv tools plugin_api tests
node --check pages/pluginCenter/app.js
node --check pages/pluginCenter/safety.mjs
node --test tests/js/safety.test.mjs
python -m pytest -v tests
```
