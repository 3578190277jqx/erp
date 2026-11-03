from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from erp import models as m, services
from .base import ERPTestCase


class ReviewRegressionTests(ERPTestCase):
    def test_assigned_surveyor_can_save_area_for_sales_owned_customer(self):
        surveyor = User.objects.create_user('surveyor')
        m.Membership.objects.create(user=surveyor, organization=self.org, role='surveyor', scope='own')
        self.customer.owner = self.sales
        self.customer.save()
        task = self.make(m.Measurement, customer=self.customer, due_date=date.today(), owner=surveyor, status='assigned')
        self.client.force_login(surveyor)
        response = self.client.post(f'/modules/measurements/{task.pk}/edit/', {'version': task.version, 'customer': self.customer.pk,
                                   'due_date': str(date.today()), 'area': '120.50', 'owner': surveyor.pk, 'note': '已现场量尺'})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.area, Decimal('120.50'))

    def test_measurement_in_progress_can_record_area_then_complete(self):
        task = self.make(m.Measurement, customer=self.customer, due_date=date.today(), status='in_progress')
        self.client.force_login(self.admin)
        response = self.client.post(f'/modules/measurements/{task.pk}/edit/', {'version': task.version, 'area': '88.20', 'note': '量尺完成'})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        services.transition(self.admin, task, 'complete', task.version, '尺寸复核通过')
        task.refresh_from_db()
        self.assertEqual(task.status, 'completed')

    def test_assigned_installer_can_view_order_and_customer_but_not_unrelated(self):
        from erp.permissions import scoped
        installer = User.objects.create_user('installer')
        m.Membership.objects.create(user=installer, organization=self.org, role='installer', scope='own')
        order = self.order(status='installing')
        self.make(m.Installation, order=order, owner=installer, due_date=date.today())
        unrelated = self.make(m.Customer, name='其他客户', phone='123')
        self.assertTrue(scoped(installer, m.Order).filter(pk=order.pk).exists())
        self.assertTrue(scoped(installer, m.Customer).filter(pk=self.customer.pk).exists())
        self.assertFalse(scoped(installer, m.Customer).filter(pk=unrelated.pk).exists())

    def test_change_submitter_cannot_bypass_by_reassigning_owner(self):
        order = self.order(status='confirmed')
        self.client.force_login(self.manager)
        response = self.client.post('/modules/changes/new/', {'organization': self.org.pk, 'owner': self.sales.pk,
                                   'order': order.pk, 'proposed_total': '1200', 'proposed_due_date': '2026-12-01',
                                   'note': '增加柜体'})
        self.assertEqual(response.status_code, 302)
        change = m.ChangeRequest.objects.get(order=order)
        with self.assertRaises(ValidationError):
            services.transition(self.manager, change, 'approve', change.version)
        order.refresh_from_db()
        self.assertEqual(order.total, Decimal('1000'))

    def test_cancelled_order_cannot_be_reactivated_by_production(self):
        order = self.order(status='cancelled')
        breakdown = self.make(m.Breakdown, order=order, status='released')
        production = self.make(m.Production, breakdown=breakdown, warehouse=self.warehouse, status='issued')
        with self.assertRaises(ValidationError):
            services.transition(self.admin, production, 'start', production.version)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')

    def test_stock_and_finance_details_show_transaction_fields(self):
        services.adjust_stock(self.admin, self.warehouse, self.board, 5, '盘点')
        order = self.order(status='confirmed')
        payment = services.record_payment(self.finance, order, 250, 'REVIEW-REF-001', date.today(), '转账', '')
        self.client.force_login(self.admin)
        response = self.client.get(f'/modules/finance/{payment.pk}/')
        self.assertContains(response, 'REVIEW-REF-001')
        self.assertContains(response, '250.00')
        stock = m.Stock.objects.get(material=self.board)
        self.assertContains(self.client.get(f'/modules/inventory/{stock.pk}/'), self.board.name)
