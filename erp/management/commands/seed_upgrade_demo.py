"""Additive examples: never overwrite v1.0 business documents or passwords."""
from datetime import timedelta
from decimal import Decimal
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand,CommandError
from django.db import transaction
from django.utils import timezone
from erp import models as m,services
from erp.services import collaboration,receivables,materials
from erp.services.common import audit


class Command(BaseCommand):
    help='为已初始化的演示库追加 v1.1 行业报价、分期、门厂交接与分批收货示例；幂等。'
    @transaction.atomic
    def handle(self,*args,**options):
        if not settings.DEBUG:raise CommandError('仅可用于本地演示环境。')
        seed=m.AuditEvent.objects.filter(action='demo_seed').first()
        if not seed:raise CommandError('请先运行 seed_demo。')
        if m.AuditEvent.objects.filter(action='demo_upgrade_v11').exists():
            self.stdout.write('v1.1 演示已存在，保留全部数据。');return
        admin=User.objects.get(username='demo_admin')
        store=m.Organization.objects.filter(kind='store',name__contains='杭州').first()
        factory=m.Organization.objects.filter(kind='factory').first()
        for role in ('planner','engineer','warehouse'):
            m.Membership.objects.get_or_create(user=User.objects.get(username=f'demo_{role}'),organization=factory,role=role,defaults={'scope':'org'})
        customer=m.Customer.objects.filter(organization=store).first()
        quote=m.Quote.objects.create(organization=store,owner=admin,customer=customer,title='白橡 · 全屋定制升级示例',discount=Decimal('.95'),tax_rate=Decimal('.13'))
        for values in [dict(name='主卧通顶衣柜',room='主卧',specification='白橡木色 / 哑光门板 / 含基础五金',pricing_mode='projected',width_mm=3200,height_mm=2600,quantity=1,unit='㎡',unit_price=1380,unit_cost=760),
                       dict(name='玄关收纳柜',room='玄关',specification='悬空柜体 / 暖光灯带',pricing_mode='projected',width_mm=1800,height_mm=2400,quantity=1,unit='㎡',unit_price=1580,unit_cost=880),
                       dict(name='安装与上楼服务',room='全屋',category='service',quantity=1,unit='项',unit_price=800,unit_cost=500,discountable=False)]:
            services.add_line(admin,quote,values,quote.version);quote.refresh_from_db()
        for action in ('submit','approve','accept'):
            services.transition(admin,quote,action,quote.version);quote.refresh_from_db()
        order=m.Order.objects.get(quote=quote)
        order.contract_number='V11-DEMO-2026';order.due_date=timezone.localdate()+timedelta(days=35);order.save()
        services.transition(admin,order,'confirm',order.version);order.refresh_from_db()
        receivables.create_plan(admin,order,order.version,timezone.localdate()+timedelta(days=1),[30,40,30]);order.refresh_from_db()
        handoff=collaboration.send_handoff(admin,order,factory,order.version,'示例：门店冻结规格后，工厂可在“门厂交接”接单。')
        m.FollowUp.objects.create(organization=store,owner=admin,customer=customer,channel='到店',next_date=timezone.localdate()+timedelta(days=3),note='已确认白橡门板与五金，等待定金到账后安排工厂生产。')
        supplier=m.Supplier.objects.create(organization=store,owner=admin,name='浙木板材（示例）',contact='许经理',phone='演示号码',payment_days=30)
        warehouse=m.Warehouse.objects.filter(organization=store).first()
        material=m.Material.objects.filter(organization=store).first()
        purchase=m.Purchase.objects.create(organization=store,owner=admin,supplier=supplier.name,supplier_record=supplier,warehouse=warehouse,due_date=timezone.localdate()+timedelta(days=7))
        services.add_line(admin,purchase,dict(material=material,quantity=100,unit_price=80),purchase.version);purchase.refresh_from_db()
        for action in ('submit','approve','order'):
            services.transition(admin,purchase,action,purchase.version);purchase.refresh_from_db()
        materials.receive_partial(admin,purchase,{purchase.lines.get().pk:40},'V11-DEMO-RECEIPT',purchase.version)
        audit(admin,order,'demo_upgrade_v11',after={'quote':quote.code,'handoff':handoff.code})
        self.stdout.write(self.style.SUCCESS('v1.1 示例已追加；原单据和密码保持不变。'))
