# 栖木家居 ERP

基于 Python / Django 的家居业务系统实践，涵盖报价、订单、BOM、采购库存、收付款和操作审计。

**完整实现位于 [feat/furniture-erp 分支](https://github.com/3578190277jqx/erp/tree/feat/furniture-erp)**。本首页用于引导阅读；开发分支尚未合并至 main。

## 重点实现

- 组织、角色与本人范围的数据权限。
- 关键业务操作的事务、并发版本校验及审计记录。
- 附件身份与权限校验、审计差异展示和回归验证。
- SQLite 本地环境、PostgreSQL 环境及 GitHub Actions 检查。

## 阅读入口

- [完整项目与启动说明](https://github.com/3578190277jqx/erp/tree/feat/furniture-erp)
- [业务源码](https://github.com/3578190277jqx/erp/tree/feat/furniture-erp/erp)
- [回归测试](https://github.com/3578190277jqx/erp/tree/feat/furniture-erp/erp/tests)
- [验证记录与边界](https://github.com/3578190277jqx/erp/blob/feat/furniture-erp/docs/VALIDATION.md)

这是项目开发与工程实践作品。界面复用了 Tabler 开源组件，不以仓库内容声称真实客户规模或商业落地效果。
