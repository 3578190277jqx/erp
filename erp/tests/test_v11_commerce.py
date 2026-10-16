from decimal import Decimal
from datetime import date
from django.core.exceptions import ValidationError
from erp import models as m, services
from .base import ERPTestCase


class CommercialUpgradeTests(ERPTestCase):
    def test_projected_area_fees_tax_and_cost_snapshot(self):
        self.assertTrue(hasattr(m, 'OrderLine'), 'Order snapshots must be implemented')
        quote = self.make(m.Quote, customer=self.customer, title='行业报价', discount=Decimal('.9'), tax_rate=Decimal('.13'))
        services.add_line(self.admin, quote, {'name':'衣柜', 'pricing_mode':'projected', 'width_mm':2000, 'height_mm':2400,
                          'quantity':2, 'unit_price':100, 'unit_cost':60, 'unit':'㎡'}, quote.version)
        quote.refresh_from_db()
        services.add_line(self.admin, quote, {'name':'安装费','quantity':1,'unit_price':200,'unit_cost':100,
                          'category':'service','discountable':False}, quote.version)
        quote.refresh_from_db()
        # 2 * 2.4 * 2 = 9.6 sqm; 960*.9 + 200 = 1064; tax 138.32.
        self.assertEqual(quote.total, Decimal('1202.32'))
        for action in ['submit','approve','accept']:
            quote.refresh_from_db()
            services.transition(self.admin, quote, action, quote.version)
        order = m.Order.objects.get(quote=quote)
        self.assertEqual(order.lines.count(), 2)
        self.assertEqual(order.lines.get(name='衣柜').billable_quantity, Decimal('9.6'))
        self.assertEqual(order.lines.get(name='衣柜').line_total, Decimal('864'))
        self.assertEqual(order.lines.get(name='衣柜').unit_cost, Decimal('60'))
        self.assertEqual(order.tax_rate, Decimal('.13'))
        quote.lines.filter(name='衣柜').update(name='旁路修改源报价')
        self.assertEqual(order.lines.get(name='衣柜').name, '衣柜')

    def test_invalid_projected_dimensions_do_not_create_line(self):
        self.assertTrue(hasattr(m, 'OrderLine'), 'Order snapshots must be implemented')
        quote = self.make(m.Quote,customer=self.customer,title='错误尺寸')
        with self.assertRaises(ValidationError):
            services.add_line(self.admin,quote,{'name':'衣柜','pricing_mode':'projected','width_mm':0,'height_mm':2000,'quantity':1,'unit_price':100},1)
        self.assertFalse(quote.lines.exists())

    def test_order_change_adds_and_removes_product_lines(self):
        self.assertTrue(hasattr(services, 'add_change_line'), 'Order change lines must be implemented')
        order = self.order(status='confirmed')
        old = m.OrderLine.objects.create(order=order,name='旧柜体',quantity=1,unit_price=1000,line_total=1000)
        change = self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=0,
                           proposed_due_date=date(2026,12,1),note='替换柜体')
        services.add_change_line(self.admin,change,{'operation':'remove','order_line':old},change.version)
        change.refresh_from_db()
        services.add_change_line(self.admin,change,{'operation':'add','name':'新柜体','quantity':2,'unit_price':700},change.version)
        change.refresh_from_db()
        self.assertEqual(change.proposed_total,Decimal('1400'))
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db()
        self.assertEqual(order.total,Decimal('1400'))
        self.assertEqual(list(order.lines.values_list('name',flat=True)),['新柜体'])

    def test_approval_rejects_tampered_total_and_keeps_product_history(self):
        order = self.order(status='confirmed')
        old = m.OrderLine.objects.create(order=order,name='旧柜体',quantity=1,unit_price=1000,line_total=1000)
        change = self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=1000,
                           proposed_due_date=date(2026,12,1),note='替换柜体')
        services.add_change_line(self.admin,change,{'operation':'update','order_line':old,'name':'改后柜体','quantity':1,'unit_price':1200},change.version)
        change.refresh_from_db()
        m.ChangeRequest.objects.filter(pk=change.pk).update(proposed_total=900)
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
        m.ChangeRequest.objects.filter(pk=change.pk).update(proposed_total=1200)
        services.transition(self.manager,change,'approve',change.version)
        event=m.AuditEvent.objects.get(object_type='orders',object_id=order.pk,action='change_products')
        self.assertEqual(event.before['lines'][0]['name'],'旧柜体')
        self.assertEqual(event.after['lines'][0]['name'],'改后柜体')
