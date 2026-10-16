# 栖木 · 家居 ERP

基于 **Django 5.2 LTS + Tabler 开源组件** 的中文家居业务系统。以全屋定制为主，覆盖客户、量尺、设计、报价、合同、拆单、BOM、生产、采购、库存、配送、安装、售后与收付款对账。

真实数据库、真实登录、服务端组织/角色/本人范围权限和事务业务流转。界面采用苹果风格系统字体、克制留白、蓝色强调、毛玻璃导航、深浅主题和紧凑密度。移动端是响应式 Web。

## 本地启动

需要 Python **3.12+**，首次安装依赖需要联网；运行页面不依赖外部 CDN，不需要 Node.js、Redis 或 Docker。

```bash
git clone https://github.com/3578190277jqx/erp.git
cd erp
git checkout feat/furniture-erp
python scripts/start.py --demo
```

Windows 也可双击 `run.bat`。访问 **http://127.0.0.1:8000**。

启动器自动创建项目 `.venv`、安装固定版本依赖、迁移数据库并初始化虚构演示数据。管理员账号 **demo_admin**，密码首次随机生成，显示在命令行，并保存在被 Git 忽略的 `.demo-credentials.txt`。没有写死的默认生产密码。`seed_demo` 重复运行不会重置密码或覆盖单据。

演示账号包括 `demo_sales`、`demo_designer`、`demo_surveyor`、`demo_engineer`、`demo_planner`、`demo_buyer`、`demo_warehouse`、`demo_logistics`、`demo_installer`、`demo_service`、`demo_finance`、`demo_manager`、`demo_boss` 和 `demo_dealer`，首次使用同一随机演示密码。杭州、宁波门店隔离，适合检查角色权限。演示数据均为虚构。

已有 Python 虚拟环境时，也可手动启动：

```bash
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo --credentials-file .demo-credentials.txt
python manage.py collectstatic --noinput
python manage.py runserver 127.0.0.1:8000
```

不需要演示数据时省略 `seed_demo`，运行 `python manage.py createsuperuser`，通过 `/admin/` 创建组织与成员。没有成员关系的普通账号不会获得业务数据。

## 业务怎么试

1. 客户详情新建报价 → 保存基本信息 → 添加产品明细 → 提交审核 → 管理角色批准 → 接受并生成订单。
2. 订单补齐合同编号与交期 → 确认合同 → 新建拆单 → 添加 BOM 物料行 → 提交 → 拆单员审核释放。
3. 系统自动生成生产工单 → 计划员编辑仓库、工作中心、日期 → 确认排程 → 仓管领料 → 计划员开工、完工。
4. 库存不足时整笔领料回滚。采购录入行 → 提交 → 店长/老板批准 → 采购下单 → 仓管收货，再尝试领料。
5. 订单生产完工后新建配送 → 派车 → 发运 → 签收 → 新建安装 → 派工 → 开始 → 填验收结论并完成。
6. 财务按订单或采购登记收付款，凭证号唯一，不能超过未结余额。订单改单通过“变更申请”，由另一管理人员审批，记录前后值和版本。

管理员可执行所有业务动作，便于演示闭环；普通角色按岗位授权。变更申请即使由管理员提交，也不能由本人审批。

## 模块与权限

| 领域 | 主要能力 |
|---|---|
| 工作台与报表 | 真实订单额、经营趋势、角色任务、交期风险、状态统计、安全库存预警 |
| 客户与成交 | 线索、客户资料、量尺、设计版本、报价明细与折扣、合同订单 |
| 制造与供应 | 拆单 BOM、物料、生产工单、人工排程看板、采购、仓库、库存及流水 |
| 交付与服务 | 物流派车及签收、安装派工与验收、失败重派、售后工单 |
| 经营与设置 | 收付款、应收应付余额、变更审批、CSV 导出、批量批准/派工、审计、组织权限 |

权限由服务器按组织成员关系与动作计算；“本人负责”“本组织”“本组织及下级”分别约束列表、详情、表单关联、报表、动作和导出。页面隐藏不是安全边界。已审核报价、已释放 BOM 及业务流水不能通过编辑表单改写。

## 验证与 CI

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py collectstatic --noinput
python manage.py test --verbosity 2
```

GitHub Actions 在 Ubuntu / Windows、Python 3.12 / 3.13 上运行相同检查，并检查生产安全配置。测试覆盖闭环、事务回滚、重复操作、权限隔离、外键篡改、CSRF、版本冲突、批量回滚、CSV 公式防护和演示数据幂等。

## 部署与数据库

本机使用 SQLite，数据库和生成密码均不提交 Git。正式部署请使用独立环境、HTTPS 反向代理并备份数据库；`runserver` 仅供开发。环境变量由进程读取，Django 不会自动读取 `.env` 文件；Docker Compose 会读取。

可选 Docker / Waitress 启动：

```bash
# 复制 .env.example 为 .env，填入随机 secret、域名与 HTTPS 来源
docker compose up --build -d
docker compose exec erp python manage.py createsuperuser
```

镜像不自动灌入演示账号。默认开启安全 Cookie 和 HTTPS 重定向，需要反向代理终止 HTTPS。设置 `DJANGO_TRUSTED_PROXY` 为 Waitress 实际看到的代理节点 IP，代理须覆盖并清理客户端传入的 `X-Forwarded-*`，再设置 `X-Forwarded-Proto=https`；不接受 `*` 通配信任。未设置代理信任时转发头会被清除。相同的生产服务器也可用 `python scripts/serve.py` 运行。SQLite 数据保存在 `erp-data` 卷。正式业务上线前请验证备份与恢复；当前开发机器未安装 Docker，容器构建状态见验证记录。

若使用 PostgreSQL，安装 `psycopg[binary]`，配置 `POSTGRES_HOST`、`POSTGRES_PORT`、`POSTGRES_DB`、`POSTGRES_USER` 和 `POSTGRES_PASSWORD` 后运行迁移。生产设置必须提供 `DJANGO_DEBUG=false`、随机 `DJANGO_SECRET_KEY`、`DJANGO_ALLOWED_HOSTS` 和 `DJANGO_CSRF_TRUSTED_ORIGINS`；支持 PostgreSQL 配置，不将未经验证的数据库部署宣称为已验收。

## 设计与交付文档

- [完整中文交互方案 v1.0](docs/superpowers/specs/2026-10-16-furniture-erp-design.md)：五阶段、流程图、12 个关键页面、权限及苹果风格细则。
- [开源调研与复用](docs/OPEN_SOURCE.md)：ERPNext / Odoo / InvenTree 的参考范围，Django / Tabler 的实际复用及许可证。
- [实施计划](docs/superpowers/plans/2026-10-16-furniture-erp.md)
- [验证记录](docs/VALIDATION.md)

## 首版边界

财务为业务收付款及对账，未实现法定总账、税务申报、电子发票或银行直连。生产为人工排程与 BOM 领料，未实现板材套裁、自动产能优化、批次追溯、部分收货或退货退款自动化。设计与验收保存结构化文字和版本，未接入图纸附件存储、电子签章或现场定位。跨组织首版为隔离业务域；门店与工厂的正式跨组织交接需要后续业务规则。

本仓库不是 ERPNext / Odoo / InvenTree 的 fork，未复制其业务源码。Tabler 1.6.1 CSS 本地复用并保留 MIT 许可证；Django 等依赖保留各自许可证。重新获取 UI 资产时才需要 npm：`npm pack @tabler/core@1.6.1` 后运行 `python scripts/vendor_assets.py tabler-core-1.6.1.tgz`。
