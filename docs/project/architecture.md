# 项目架构

## 入口层

`main.py` 负责 AstrBot 生命周期、命令装饰器、公共配置读取和领域对象装配。签到与图片来源的复杂流程由领域 Mixin 实现，入口类直接组合这些 Mixin，不使用动态属性代理。

## 领域模块

```text
checkin/
│  ├─ models.py          数据模型与成就定义
│  ├─ rules.py           金币、好感度、连签和加持规则
│  ├─ snapshot.py        schema v6 快照校验
│  ├─ schema.py          SQLite 建表与版本校验
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
│  ├─ downloader.py      图片下载与质量降级
│  ├─ index.py           去重索引、安全词与作品黑名单
plugin_api/
└─ api.py                Plugin Pages 后端 API
```

根目录不再保留单文件业务实现或兼容 wrapper。`__init__.py` 集中注册旧包路径别名，已有预览脚本可以继续使用，但新代码和测试必须直接导入领域包。

模块按职责划分，不按行数强制拆分。`__init__.py` 和组合入口可以很小；普通业务模块只有在具备独立职责和测试边界时才单独存在。

## 签到指令入口

签到业务只保留 `/签到` 和 `/签到中心` 两个顶层入口；纯文本“签到”继续由正则触发。`/签到中心` 使用 AstrBot 原生嵌套指令组，按“我的、排行、商店、管理”组织功能，旧平铺指令和静态帮助图不再保留。指令装饰器集中在 `main.py`，处理函数继续委托给 `CheckinCommandMixin` 与 `CheckinShopMixin`，避免命令结构与业务实现相互耦合。

## 签到商店扩展

`checkin/shop.py` 中的 `build_checkin_shop_items()` 是商品展示目录的统一注册点，每个商品拥有稳定 `item_id`、分类、指令、名称和价格。新增商品时先在目录中注册展示项，再在 `CheckinShopMixin` 增加购买处理，并将需要原子扣款的数据操作放入 `CheckinStore` 对应 store 模块；入口层只保留 AstrBot 指令装饰器。商品目录和购买行为应分别补充测试。

## 前端页面

`pages/pluginCenter/` 使用原生 HTML、CSS 和 ES module，集中提供群排行、成员当前数值编辑、内容安全和签到数据管理。前端不持久化业务数据；SQLite 和签到备份仍是后端唯一数据源。成员编辑只更新 `checkin_users`，不回写 `checkin_records` 或 `checkin_group_presence`。

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
python -m compileall -q main.py checkin pixiv plugin_api tests
node --check pages/pluginCenter/app.js
python -m pytest -q
```
