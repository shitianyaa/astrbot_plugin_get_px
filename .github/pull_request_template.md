## 变更说明

<!-- 简明描述本次改了什么、为什么改。 -->

## 关联

- 修复 issue：#
- 关联的 Roadmap 方向：

## 改动范围

<!-- 列出主要改动文件/模块，以及是否涉及配置、数据库 schema、文档同步。 -->

- [ ] 新增/修改功能：
- [ ] 配置项（`_conf_schema.json`）：
- [ ] 数据库 schema / 迁移：
- [ ] 文档同步（README / docs）：

## 验证

- [ ] `python -m compileall -q main.py checkin pixiv plugin_api scripts/ci tests`
- [ ] `python -m json.tool _conf_schema.json`
- [ ] `node --check pages/pluginCenter/app.js`
- [ ] `pytest` 全绿
- [ ] 手工验证（如适用）：

## 风险与兼容性

- 向后兼容性：
- 已知风险：
- 是否影响旧数据 / 旧配置：