import os
import secrets
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from erp import models as m
from erp import services
from erp.services.common import audit


class Command(BaseCommand):
    help = '初始化两个隔离的虚构门店、业务闭环与角色账号；重复运行不会覆盖数据或密码。'

    def add_arguments(self, parser):
        parser.add_argument('--credentials-file', help='仅首次创建时，将演示账号密码写入指定本地文件。不要提交该文件。')

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('演示数据命令仅在 DJANGO_DEBUG=true 的本地环境可用。正式环境使用 createsuperuser。')
        if m.AuditEvent.objects.filter(action='demo_seed').exists():
            self.stdout.write('演示数据已存在：保持业务数据与所有账号密码不变。')
            return
        password = os.getenv('ERP_DEMO_PASSWORD') or secrets.token_urlsafe(15)
        root = m.Organization.objects.create(name='栖木家居（示例）', kind='company')
        store = m.Organization.objects.create(name='杭州西湖店（示例）', kind='store', parent=root)
        other = m.Organization.objects.create(name='宁波鄞州店（示例）', kind='store', parent=root)
        m.Organization.objects.create(name='杭州定制工厂（示例）', kind='factory', parent=root)
        if User.objects.filter(username='demo_admin').exists():
            raise CommandError('demo_admin 已存在且未初始化演示数据。请使用空演示数据库，避免覆盖现有账号。')
        admin = User.objects.create_superuser('demo_admin', password=password, first_name='林知行')
        names = {'boss': '陈总', 'manager': '沈店长', 'sales': '顾安然', 'designer': '林予初',
                 'surveyor': '周师傅', 'engineer': '赵工', 'planner': '程计划', 'buyer': '许亦辰',
                 'warehouse': '王仓管', 'logistics': '郑司机', 'installer': '吴师傅', 'service': '苏售后',
                 'finance': '宋会计', 'dealer': '宁波经销商'}
        users = {'admin': admin}
        for role, label in m.ROLES:
            username = f'demo_{role}'
            if User.objects.filter(username=username).exists():
                raise CommandError(f'{username} 已存在，请在独立演示数据库初始化。')
            user = User.objects.create_user(username, password=password, first_name=names[role])
            org = other if role == 'dealer' else (root if role == 'boss' else store)
            scope = 'tree' if role == 'boss' else ('own' if role in ('sales', 'designer', 'surveyor', 'installer', 'logistics', 'dealer') else 'org')
            m.Membership.objects.create(user=user, organization=org, role=role, scope=scope)
            users[role] = user
        today = timezone.localdate()

        def make(model, **values):
            owner = values.pop('owner', admin)
            obj = model.objects.create(organization=store, owner=owner, **values)
            audit(admin, obj, 'demo_create')
            return obj

        def act(obj, action, note=''):
            obj.refresh_from_db()
            services.transition(admin, obj, action, obj.version, note)
            obj.refresh_from_db()

        warehouse = make(m.Warehouse, name='杭州中心仓', address='杭州市余杭区家居产业园 8 号')
        materials = [make(m.Material, name=name, specification=spec, unit=unit, unit_cost=cost, safety_stock=safety)
                     for name, spec, unit, cost, safety in [
                         ('自然白橡饰面板', '2440 × 1220 × 18 mm · ENF 级', '张', 268, 50),
                         ('雾白 PET 门板', '2440 × 1220 × 18 mm · 肤感哑光', '张', 395, 30),
                         ('缓冲铰链', '110° · 全盖 · 镀镍', '件', 18, 100),
                         ('三节静音滑轨', '450 mm · 缓冲', '副', 68, 30),
                         ('暖白灯带', '3000 K · 24 V', '米', 35, 100),
                     ]]
        for material, quantity in zip(materials, [360, 150, 800, 18, 40]):
            services.adjust_stock(admin, warehouse, material, quantity, '演示环境期初库存（虚构数据）')
        customers = []
        for i, (name, project, area, stage) in enumerate([
            ('林女士', '云栖望月 · 全屋定制', '128', 'confirmed'),
            ('陈先生', '和光尘樾 · 原木生活', '156', 'producing'),
            ('许女士', '春江花月 · 轻盈之境', '108', 'ready'),
            ('周先生', '滨江壹号 · 雅致空间', '186', 'shipping'),
            ('沈女士', '晓风印月 · 温润之家', '139', 'installing'),
            ('方先生', '绿城桃源 · 理想居所', '220', 'completed'),
            ('陆女士', '桂语江南 · 一家四季', '118', 'confirmed'),
        ]):
            customer = make(m.Customer, name=name, phone=f'1380000{1000+i:04d}',
                            address=f'杭州市西湖区{project.split(" · ")[0]} {i+1} 幢 1201', source=['门店到访', '老客转介绍', '线上预约'][i % 3],
                            due_date=today + timedelta(days=i+1), owner=users['sales'])
            customers.append(customer)
            measure = make(m.Measurement, customer=customer, due_date=today - timedelta(days=10), area=area, owner=users['surveyor'])
            for action in ('assign', 'start', 'complete'):
                act(measure, action, '已核对墙体、梁柱及水电点位，尺寸复核通过。' if action == 'complete' else '')
            design = make(m.Design, customer=customer, title=project, note='自然白橡 + 雾白门板；客餐厅与卧室一体化收纳。', owner=users['designer'])
            act(design, 'submit')
            act(design, 'approve')
            quote = make(m.Quote, customer=customer, design=design, title=project, discount=Decimal('.95'), owner=users['sales'])
            for line in [{'name': '客餐厅一体柜', 'room': '客餐厅', 'specification': '白橡饰面 / 无拉手', 'quantity': Decimal('8.5'), 'unit': '㎡', 'unit_price': Decimal('1680')},
                         {'name': '主卧衣帽系统', 'room': '主卧', 'specification': '雾白 PET / 铝框玻璃', 'quantity': Decimal(12+i), 'unit': '㎡', 'unit_price': Decimal('1980')},
                         {'name': '全屋安装服务', 'room': '全屋', 'quantity': Decimal(1), 'unit': '项', 'unit_price': Decimal('2800')}]:
                quote.refresh_from_db()
                services.add_line(admin, quote, line, quote.version)
            for action in ('submit', 'approve', 'accept'):
                act(quote, action)
            order = m.Order.objects.get(quote=quote)
            order.contract_number = f'QM-{today.year}-{i+1:04d}'
            order.due_date = today + timedelta(days=[8, 12, 2, 1, -1, -3, 20][i])
            order.save()
            act(order, 'confirm')
            services.record_payment(admin, order, Decimal('10000'), f'DEMO-IN-{i+1}', today - timedelta(days=5), '银行转账', '演示定金')
            if stage != 'confirmed':
                breakdown = make(m.Breakdown, order=order, owner=users['engineer'], note='按确认方案生产；封边色号与饰面一致。')
                for material, quantity in [(materials[0], Decimal('8.2')), (materials[1], Decimal('4')), (materials[2], Decimal('24'))]:
                    breakdown.refresh_from_db()
                    services.add_line(admin, breakdown, {'material': material, 'quantity': quantity, 'waste': Decimal('.05')}, breakdown.version)
                act(breakdown, 'submit')
                act(breakdown, 'release')
                production = m.Production.objects.get(breakdown=breakdown)
                production.owner = users['planner']
                production.warehouse = warehouse
                production.start_date = today - timedelta(days=4)
                production.due_date = today + timedelta(days=3-i)
                production.work_center = '定制柜体一线' if i % 2 else '精装木作二线'
                production.save()
                for action in ('plan', 'issue', 'start'):
                    act(production, action)
                if stage != 'producing':
                    act(production, 'complete')
                if stage in ('shipping', 'installing', 'completed'):
                    delivery = make(m.Delivery, order=order, due_date=today + timedelta(days=1), vehicle=f'浙A·QM{i+1:03d}', owner=users['logistics'])
                    act(delivery, 'assign')
                    act(delivery, 'ship')
                    if stage in ('installing', 'completed'):
                        act(delivery, 'deliver', '包装完好，客户签收。')
                        install = make(m.Installation, order=order, due_date=today + timedelta(days=i-5), owner=users['installer'])
                        act(install, 'assign')
                        if stage == 'completed':
                            act(install, 'start')
                            act(install, 'complete', '柜体水平、门板间隙与五金调试验收通过。')
        # Open tasks make each specialist's workspace useful on first login.
        new_customer = make(m.Customer, name='江女士', phone='13900001234', address='杭州市拱墅区', source='设计师推荐', status='contacted', owner=users['sales'])
        make(m.Measurement, customer=new_customer, due_date=today, owner=users['surveyor'], note='预约上午 10 点，需提前电话确认。')
        make(m.Design, customer=new_customer, title='河畔花园 · 新家方案', owner=users['designer'])
        draft = make(m.Quote, customer=new_customer, title='河畔花园 · 初步报价', owner=users['sales'])
        services.add_line(admin, draft, {'name': '玄关与衣柜系统', 'quantity': Decimal('15'), 'unit': '㎡', 'unit_price': Decimal('1580')}, draft.version)
        purchase = make(m.Purchase, supplier='森源板材（示例供应商）', warehouse=warehouse, due_date=today + timedelta(days=4), owner=users['buyer'])
        services.add_line(admin, purchase, {'material': materials[3], 'quantity': Decimal('50'), 'unit_price': Decimal('68')}, purchase.version)
        for action in ('submit', 'approve', 'order'):
            act(purchase, action)
        services.record_payment(admin, purchase, Decimal('1000'), 'DEMO-OUT-001', today, '银行转账', '演示采购预付款')
        completed = m.Order.objects.filter(organization=store, status='completed').first()
        make(m.AfterSales, order=completed, title='主卧柜门缓冲力度复调', priority='normal', due_date=today + timedelta(days=2), owner=users['service'])
        confirmed = m.Order.objects.filter(organization=store, status='confirmed').first()
        make(m.ChangeRequest, order=confirmed, proposed_total=confirmed.total + 1800,
             proposed_due_date=confirmed.due_date + timedelta(days=3), order_version=confirmed.version,
             note='客户新增玄关开放格，需要调整报价与交期。', owner=users['sales'])
        other_customer = m.Customer.objects.create(organization=other, owner=users['dealer'], name='宁波演示客户', phone='13700000001', status='contacted')
        m.Order.objects.create(organization=other, owner=users['dealer'], customer=other_customer, title='宁波 · 经销商项目', total=28600,
                               status='confirmed', contract_number='NB-DEMO-001', due_date=today + timedelta(days=14))
        audit(admin, customers[0], 'demo_seed', after={'notice': 'All demo business data is fictional.'})
        accounts = ['demo_admin', *[f'demo_{role}' for role, _ in m.ROLES]]
        content = '栖木 ERP 本地演示账号（切勿用于正式环境）\n' + '\n'.join(accounts) + f'\n首次生成的演示密码：{password}\n'
        if options.get('credentials_file'):
            Path(options['credentials_file']).write_text(content, encoding='utf-8')
        self.stdout.write(self.style.SUCCESS('演示环境已初始化：两个隔离门店、14 个岗位、全流程单据。'))
        self.stdout.write(content)
