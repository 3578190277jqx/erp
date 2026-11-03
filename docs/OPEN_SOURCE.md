# 开源项目调研与复用记录

调研日期：2026-10-16。所有项目以官方 GitHub 仓库为依据。

| 项目 | 官方源码 | 优势 | 本项目的使用方式 |
|---|---|---|---|
| ERPNext | https://github.com/frappe/erpnext | CRM、报价、BOM、生产、采购、库存和财务集成；GPL-3.0 | 业务模块划分参考；没有复制源码，也不是 ERPNext 分支 |
| Odoo | https://github.com/odoo/odoo | 模块化 ERP，销售与制造链路完整 | 单据关联与模块边界参考；没有复制源码 |
| InvenTree | https://github.com/inventree/InvenTree | Python 库存、物料、BOM 与采购 | 库存流水及 BOM 设计参考；没有复制源码 |
| Tabler | https://github.com/tabler/tabler | MIT 许可，成熟的 Bootstrap 后台组件 | **实际复用** `@tabler/core` 1.6.1 的发行 CSS；本地托管，保留许可证 |
| Django | https://github.com/django/django | BSD 许可，认证、CSRF、ORM、迁移、表单与管理后台 | **实际依赖** Django 5.2 LTS；业务服务、页面与测试在本仓库开发 |

## 选型结论

这是基于 Django 和 Tabler 开源组件构建的家居 ERP，不是把现成 ERP 换皮后宣称全部自研。现有 Windows 环境未安装 Docker，ERPNext 的 MariaDB / Redis / Bench 运行栈会增加本机验证成本。采用单体应用：Django 模板 + 原生 JavaScript + 本地 Tabler CSS，SQLite 用于本机首版，生产可通过配置使用 PostgreSQL。

数据库包含业务实体、关联外键、事务、状态机和服务器端权限。不会用浏览器 localStorage 模拟业务数据库；localStorage 仅保存主题、密度和侧栏偏好。

Tabler 依赖通过 npm 固定版本获取，`scripts/vendor_assets.py` 将 CSS 和许可证复制到版本控制内。浏览业务页面不需要连接外部 CDN。

## 当前范围

可独立运行的业务首版，覆盖线索到交付与对账的闭环。财务为收付款登记与应收应付对账，不包含法定总账、报税、银行直连。移动端为响应式 Web，不冒充已发布的 App 或微信小程序。高级自动排程、板材套裁和硬件对接留有明确边界。


## v1.1 市场对标

2026-10-16 补充核对三维家云订单与 ERPNext 的库存预留、付款条款、采购收货官方文档。资料链接、落地方式及未实现范围见 [评审采纳记录](V11_REVIEW_RESPONSE.md)。本轮继续开发现有 Django 业务服务，没有复制竞品源码或宣称已集成其平台。
