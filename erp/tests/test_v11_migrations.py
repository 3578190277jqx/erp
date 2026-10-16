"""Upgrade real historical rows, not just an empty database schema."""
from decimal import Decimal
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HistoricalUpgradeTests(TransactionTestCase):
    latest=('erp','0007_backfill_execution_history')

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.latest])
        super().tearDown()

    def test_preserves_contract_and_backfills_received_quantities_and_design_families(self):
        executor=MigrationExecutor(connection)
        old=('erp','0002_changerequest_requested_by')
        executor.migrate([old])
        apps=executor.loader.project_state([old]).apps
        user=apps.get_model('auth','User').objects.create(username='historical',password='!')
        org=apps.get_model('erp','Organization').objects.create(name='历史门店')
        common={'organization_id':org.pk,'owner_id':user.pk}
        customer=apps.get_model('erp','Customer').objects.create(**common,code='OLD-C',name='历史客户',phone='123')
        quote=apps.get_model('erp','Quote').objects.create(**common,code='OLD-Q',customer_id=customer.pk,title='历史报价',discount=Decimal('.9'),total=900)
        apps.get_model('erp','QuoteLine').objects.create(quote_id=quote.pk,name='原产品',quantity=2,unit_price=500)
        order=apps.get_model('erp','Order').objects.create(**common,code='OLD-O',customer_id=customer.pk,quote_id=quote.pk,title='历史变更订单',total=850)
        warehouse=apps.get_model('erp','Warehouse').objects.create(**common,code='OLD-W',name='历史仓')
        material=apps.get_model('erp','Material').objects.create(**common,code='OLD-M',name='历史物料')
        purchase=apps.get_model('erp','Purchase').objects.create(**common,code='OLD-P',supplier='历史供应商',warehouse_id=warehouse.pk,status='received')
        row=apps.get_model('erp','PurchaseLine').objects.create(purchase_id=purchase.pk,material_id=material.pk,quantity=10,unit_price=100)
        for code in ('OLD-D1','OLD-D2'):
            apps.get_model('erp','Design').objects.create(**common,code=code,customer_id=customer.pk,title=code)
        executor=MigrationExecutor(connection);executor.migrate([self.latest])
        from erp import models as m
        self.assertEqual(m.Order.objects.get(pk=order.pk).total,850)
        self.assertEqual(m.OrderLine.objects.get(order_id=order.pk).line_total,900)
        self.assertEqual(m.OrderLine.objects.get(order_id=order.pk).name,'原产品')
        self.assertEqual(m.PurchaseLine.objects.get(pk=row.pk).received_qty,10)
        self.assertEqual(m.StockMovement.objects.count(),0)
        self.assertEqual(len(set(m.Design.objects.values_list('family_id',flat=True))),2)
