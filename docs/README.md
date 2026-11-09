# GetPx 插件文档索引

`astrbot_plugin_get_px` 是中型 AstrBot 插件，文档分为 `project/`、`dev/` 和 `user/` 三类。用户使用说明以根目录 [`README.md`](../README.md) 为准。

## 文档职责边界

- `docs/README.md` 只做总索引和阅读路径。
- `docs/dev/` 只写开发、测试、贡献和维护纪律，不复制业务语义。
- `docs/project/overview.md` 只写插件定位、能力边界和当前入口。
- `docs/project/architecture.md` 写模块关系、主要流程和安全边界。
- `docs/project/configuration.md` 是配置字段语义的细节来源。
- `docs/project/cache-and-dedupe.md` 维护缓存身份、有效期、去重范围、幂等与清理边界。
- `docs/user/` 是用户功能使用说明，包括签到和指令参考。

如果多个文档都提到同一概念，应由最具体的专题文档维护细节，其他文档只链接过去。

## 章节索引

### project

- [`project/overview.md`](./project/overview.md): 项目定位、能力边界和当前入口
- [`project/architecture.md`](./project/architecture.md): 模块关系、主要流程和安全边界
- [`project/configuration.md`](./project/configuration.md): `_conf_schema.json` 字段语义、默认值和文档同步要求
- [`project/cache-and-dedupe.md`](./project/cache-and-dedupe.md): 缓存复用、图片去重、幂等与失效条件

### dev

- [`dev/setup.md`](./dev/setup.md): 环境准备、常用目录、运行与调试入口
- [`dev/testing.md`](./dev/testing.md): 语法检查、pytest 和回归建议
- [`dev/contributing.md`](./dev/contributing.md): 贡献流程、改动边界和提交流程
- [`dev/engineering-principles.md`](./dev/engineering-principles.md): 工具使用、错误处理、保护逻辑和验证原则
- [`dev/maintenance.md`](./dev/maintenance.md): 维护规则、配置边界和文档同步要求

### user

- [`user/checkin.md`](./user/checkin.md): 签到功能说明、金币商店和好感度系统
- [`user/commands.md`](./user/commands.md): 全部指令参考

## 推荐阅读路径

### 我想快速理解这个插件

1. [`project/overview.md`](./project/overview.md)
2. [`project/architecture.md`](./project/architecture.md)
3. [`README.md`](../README.md)

### 我要参与开发或维护

1. [`dev/setup.md`](./dev/setup.md)
2. [`dev/testing.md`](./dev/testing.md)
3. [`dev/contributing.md`](./dev/contributing.md)
4. [`dev/engineering-principles.md`](./dev/engineering-principles.md)
5. [`dev/maintenance.md`](./dev/maintenance.md)

### 我要改配置或 README

1. [`project/configuration.md`](./project/configuration.md)
2. [`_conf_schema.json`](../_conf_schema.json)
3. [`README.md`](../README.md)

### 我要排查缓存或重复图片

1. [`project/cache-and-dedupe.md`](./project/cache-and-dedupe.md)
2. [`dev/testing.md`](./dev/testing.md#缓存复用与去重回归)

### 我要了解用户功能

1. [`user/commands.md`](./user/commands.md)
2. [`user/checkin.md`](./user/checkin.md)
