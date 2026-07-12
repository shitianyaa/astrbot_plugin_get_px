# 项目架构

## 入口层

`main.py` 负责 AstrBot 生命周期、命令装饰器、公共配置读取和领域对象装配。签到与 Pixiv 的复杂流程由领域 Mixin 实现，入口类直接组合这些 Mixin，不使用动态属性代理。

## 领域模块

```text
checkin/
│  ├─ models.py          数据模型与成就定义
│  ├─ rules.py           金币、好感度、连签和加持规则
│  ├─ snapshot.py        V1/V2/V3 快照校验
│  ├─ schema.py          SQLite 建表与字段迁移
│  ├─ record_store.py    签到资料、奖励和卡片记录
│  ├─ feature_store.py   生日、成就、称号和全局事件
│  ├─ backup_store.py    快照导入导出
│  ├─ store.py           CheckinStore 组合入口
│  ├─ application.py     每日签到流程与问候内容
│  ├─ commands.py        签到功能命令业务
│  ├─ artwork.py         卡片渲染与背景作品选择
│  └─ holiday.py         联网节假日数据更新与查询
pixiv/
│  ├─ search.py          搜索、排行和作品列表流程
│  ├─ delivery.py        详情、下载与消息发送
│  ├─ filters.py         R18、漫画和黑名单过滤
│  ├─ client.py          Pixiv API 客户端
│  ├─ downloader.py      图片下载与质量降级
│  ├─ history.py         已发送图片历史
│  ├─ index.py           去重索引与作品黑名单
│  └─ commenter.py       AI 识图与评论
plugin_api/
└─ api.py                Plugin Pages 后端 API
```

根目录不再保留单文件业务实现或兼容 wrapper。`__init__.py` 集中注册旧包路径别名，已有预览脚本可以继续使用，但新代码和测试必须直接导入领域包。

模块按职责划分，不按行数强制拆分。`__init__.py` 和组合入口可以很小；普通业务模块只有在具备独立职责和测试边界时才单独存在。

## 前端页面

`pages/imageHistory/` 使用原生模块：

- `js/core.js`：bridge、状态和 DOM 引用。
- `js/render.js`：筛选与页面渲染。
- `js/actions.js`：删除、黑名单、复制和确认框。
- `js/data.js`：API 加载、缩略图和签到备份导入。
- `js/app.js`：事件绑定与启动。
- `css/`：设计变量、布局、图库、弹层和响应式样式。

前端不持久化签到或图片数据；SQLite、图片历史目录和卡片缓存仍是后端唯一数据源。

## 依赖方向

```text
main.py
  → checkin / pixiv / plugin_api
    → rules / stores / renderers / clients
      → SQLite、文件系统、Pixiv、AstrBot、Hitokoto
```

数据模型和规则不依赖 AstrBot 事件对象。AstrBot 事件、消息链和 Plugin Pages bridge 只出现在入口、服务与 Web API 层。

## 验证

```powershell
python -m json.tool _conf_schema.json
python -m compileall -q main.py checkin pixiv plugin_api tests
node --check pages/imageHistory/js/core.js
node --check pages/imageHistory/js/render.js
node --check pages/imageHistory/js/actions.js
node --check pages/imageHistory/js/data.js
node --check pages/imageHistory/js/app.js
python -m pytest -q --ignore=tests/test_offset.py
```
