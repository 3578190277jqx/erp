from datetime import date
from decimal import Decimal
from django.core.exceptions import ValidationError
from erp import models as m
from erp import services
from .base import ERPTestCase


class StockFinanceTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(hasattr(services, 'adjust_stock'), 'Stock transaction service must be implemented')

    def test_shortage_rolls_back_every_material(self):
        order = self.order(status='confirmed')
        breakdown = self.make(m.Breakdown, order=order, status='released')
        m.BOMLine.objects.create(breakdown=breakdown, material=self.board, quantity=3)
        m.BOMLine.objects.create(breakdown=breakdown, material=self.hinge, quantity=5)
        production = self.make(m.Production, breakdown=breakdown, warehouse=self.warehouse, status='planned')
        services.adjust_stock(self.admin, self.warehouse, self.board, 10, '盘点')
        before = m.StockMovement.objects.count()
        with self.assertRaises(ValidationError):
            services.transition(self.admin, production, 'issue', production.version)
        self.assertEqual(m.Stock.objects.get(material=self.board).quantity, Decimal('10'))
        self.assertEqual(m.StockMovement.objects.count(), before)
        production.refresh_from_db()
        self.assertEqual(production.status, 'planned')

    def test_receive_is_not_repeatable(self):
        purchase = self.make(m.Purchase, supplier='森源板材', warehouse=self.warehouse)
        services.add_line(self.buyer, purchase, {'material': self.board, 'quantity': Decimal('5'), 'unit_price': Decimal('100')}, purchase.version)
        for action, user in [('submit', self.buyer), ('approve', self.manager), ('order', self.buyer), ('receive', self.admin)]:
            purchase.refresh_from_db()
            services.transition(user, purchase, action, purchase.version)
        purchase.refresh_from_db()
        with self.assertRaises(ValidationError):
            services.transition(self.admin, purchase, 'receive', purchase.version)
        self.assertEqual(m.Stock.objects.get(material=self.board).quantity, Decimal('5'))
        self.assertEqual(m.StockMovement.objects.count(), 1)

    def test_adjustment_requires_reason_and_cannot_go_negative(self):
        for value, note in [(0, '盘点'), (-1, '盘点'), (1, ''), ('NaN', '盘点'), ('Infinity', '盘点')]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                services.adjust_stock(self.admin, self.warehouse, self.board, value, note)
        self.assertFalse(m.StockMovement.objects.exists())

    def test_overpayment_duplicate_reference_and_nonfinite_are_rejected(self):
        order = self.order(status='confirmed')
        services.record_payment(self.finance, order, 600, 'IN-001', date.today(), '银行转账', '')
        for value, reference in [(401, 'IN-002'), (1, 'IN-001'), (-1, 'IN-003'), ('NaN', 'IN-004')]:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                services.record_payment(self.finance, order, value, reference, date.today(), '银行转账', '')
        self.assertEqual(m.Payment.objects.count(), 1)
        self.assertEqual(m.Payment.objects.get().amount, Decimal('600'))

    def test_unconfirmed_order_cannot_receive_payment(self):
        with self.assertRaises(ValidationError):
            services.record_payment(self.finance, self.order(), 10, 'IN-005', date.today(), '银行转账', '')
