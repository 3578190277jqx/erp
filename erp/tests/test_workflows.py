from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError, PermissionDenied
from erp import models as m
from erp import services
from .base import ERPTestCase


class WorkflowTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(hasattr(services, 'transition'), 'Explicit workflow service must be implemented')

    def act(self, obj, action, user=None, note=''):
        obj.refresh_from_db()
        result = services.transition(user or self.admin, obj, action, obj.version, note)
        obj.refresh_from_db()
        return result

    def quote(self):
        quote = self.make(m.Quote, customer=self.customer, title='全屋方案', discount=Decimal('.9'))
        services.add_line(self.admin, quote, {'name': '衣柜', 'quantity': Decimal('2'), 'unit_price': Decimal('500.25')}, quote.version)
        quote.refresh_from_db()
        return quote

    def test_quote_total_approval_and_unique_conversion(self):
        quote = self.quote()
        self.assertEqual(quote.total, Decimal('900.45'))
        self.act(quote, 'submit')
        self.act(quote, 'approve', self.manager)
        self.act(quote, 'accept', self.sales)
        order = m.Order.objects.get(quote=quote)
        self.assertEqual(order.total, Decimal('900.45'))
        self.assertEqual(order.status, 'draft')
        with self.assertRaises(ValidationError):
            self.act(quote, 'accept', self.sales)
        self.assertEqual(m.Order.objects.filter(quote=quote).count(), 1)
        self.assertTrue(m.AuditEvent.objects.filter(code=quote.code, action='accept').exists())

    def test_empty_quote_cannot_submit_and_approved_lines_are_frozen(self):
        quote = self.make(m.Quote, customer=self.customer, title='空报价')
        with self.assertRaises(ValidationError):
            self.act(quote, 'submit')
        quote = self.quote()
        self.act(quote, 'submit')
        with self.assertRaises(ValidationError):
            services.add_line(self.admin, quote, {'name': '门板', 'quantity': 1, 'unit_price': 100}, quote.version)

    def test_stale_version_rejected_without_effects(self):
        quote = self.quote()
        old = quote.version
        self.act(quote, 'submit')
        with self.assertRaises(ValidationError):
            services.transition(self.manager, quote, 'approve', old)
        quote.refresh_from_db()
        self.assertEqual(quote.status, 'review')

    def test_buyer_cannot_approve_and_other_org_cannot_transition(self):
        quote = self.quote()
        self.act(quote, 'submit')
        with self.assertRaises(PermissionDenied):
            self.act(quote, 'approve', self.buyer)
        other = m.Organization.objects.create(name='外部门店')
        quote.organization = other
        quote.save()
        with self.assertRaises(PermissionDenied):
            self.act(quote, 'approve', self.manager)

    def test_full_production_delivery_installation_lifecycle(self):
        order = self.order()
        self.act(order, 'confirm')
        breakdown = self.make(m.Breakdown, order=order, quantity=2)
        services.add_line(self.admin, breakdown, {'material': self.board, 'quantity': Decimal('3'), 'waste': Decimal('.1')}, breakdown.version)
        self.act(breakdown, 'submit')
        self.act(breakdown, 'release')
        production = m.Production.objects.get(breakdown=breakdown)
        production.warehouse = self.warehouse
        production.start_date = date(2026, 10, 20)
        production.due_date = date(2026, 10, 25)
        production.save()
        self.act(production, 'plan')
        services.adjust_stock(self.admin, self.warehouse, self.board, Decimal('10'), '期初盘点')
        self.act(production, 'issue')
        self.assertEqual(m.Stock.objects.get(material=self.board).quantity, Decimal('3.400'))
        self.act(production, 'start')
        order.refresh_from_db()
        self.assertEqual(order.status, 'producing')
        self.act(production, 'complete')
        delivery = self.make(m.Delivery, order=order, due_date=date(2026, 10, 26), vehicle='浙A12345')
        self.act(delivery, 'assign')
        self.act(delivery, 'ship')
        self.act(delivery, 'deliver', note='客户已签收')
        install = self.make(m.Installation, order=order, due_date=date(2026, 10, 27))
        self.act(install, 'assign')
        self.act(install, 'start')
        self.act(install, 'complete', note='柜门调平，客户验收通过')
        order.refresh_from_db()
        self.assertEqual(order.status, 'completed')

    def test_installation_and_delivery_cannot_skip_prerequisites(self):
        order = self.order(status='confirmed')
        delivery = self.make(m.Delivery, order=order, due_date=date.today(), status='assigned')
        with self.assertRaises(ValidationError):
            self.act(delivery, 'ship')
        install = self.make(m.Installation, order=order, due_date=date.today(), status='assigned')
        with self.assertRaises(ValidationError):
            self.act(install, 'start')

    def test_change_requires_other_approver_and_tracks_old_and_new(self):
        order = self.order(status='confirmed')
        change = self.make(m.ChangeRequest, order=order, proposed_total=1200,
                           proposed_due_date=date(2026, 12, 1), order_version=order.version, note='增加玄关柜')
        with self.assertRaises(ValidationError):
            self.act(change, 'approve')
        self.act(change, 'approve', self.manager)
        order.refresh_from_db()
        self.assertEqual(order.total, Decimal('1200'))
        event = m.AuditEvent.objects.get(code=order.code, action='approved_change')
        self.assertEqual(event.before['total'], '1000.00')
        self.assertEqual(event.after['total'], '1200.00')

    def test_cancellation_after_release_is_rejected(self):
        order = self.order(status='confirmed')
        self.make(m.Breakdown, order=order, status='released')
        with self.assertRaises(ValidationError):
            self.act(order, 'cancel', note='客户退单')

    def test_foreign_material_rejected_in_bom(self):
        other = m.Organization.objects.create(name='别的组织')
        self.board.organization = other
        self.board.save()
        breakdown = self.make(m.Breakdown, order=self.order(status='confirmed'))
        with self.assertRaises(ValidationError):
            services.add_line(self.admin, breakdown, {'material': self.board, 'quantity': 1}, breakdown.version)
