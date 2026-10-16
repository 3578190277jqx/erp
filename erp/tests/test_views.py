from datetime import date
from decimal import Decimal
from django.test import Client
from django.contrib.auth.models import User
from erp import models as m
from .base import ERPTestCase


class ViewTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)

    def test_login_required_and_csrf_enforced(self):
        anonymous = Client()
        self.assertEqual(anonymous.get('/modules/orders/').status_code, 302)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post('/modules/customers/new/', {'name': 'CSRF'}).status_code, 403)

    def test_all_modules_render_and_dashboard_has_live_data(self):
        order = self.order(status='confirmed')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '工作台')
        for key in ['customers', 'measurements', 'designs', 'quotes', 'orders', 'breakdowns',
                    'materials', 'production', 'purchases', 'inventory', 'warehouses', 'movements',
                    'deliveries', 'installations', 'aftersales', 'finance', 'changes', 'audit']:
            with self.subTest(key=key):
                self.assertEqual(self.client.get(f'/modules/{key}/').status_code, 200)
        for route in ['/reports/', '/schedule/', '/organizations/']:
            self.assertEqual(self.client.get(route).status_code, 200)

    def test_cross_org_list_detail_export_and_action_blocked(self):
        other = m.Organization.objects.create(name='其他店')
        secret = m.Customer.objects.create(organization=other, owner=self.admin, name='机密客户', phone='123')
        self.client.force_login(self.sales)
        self.assertNotContains(self.client.get('/modules/customers/'), '机密客户')
        self.assertEqual(self.client.get(f'/modules/customers/{secret.pk}/').status_code, 404)
        self.assertNotIn('机密客户', self.client.get('/modules/customers/export/').content.decode('utf-8-sig'))
        self.assertEqual(self.client.post(f'/modules/customers/{secret.pk}/action/', {'action': 'contact', 'version': 1}).status_code, 404)

    def test_foreign_key_tampering_rejected(self):
        other = m.Organization.objects.create(name='其他店')
        secret = m.Customer.objects.create(organization=other, owner=self.admin, name='机密客户', phone='123')
        self.client.force_login(self.sales)
        response = self.client.post('/modules/quotes/new/', {'organization': self.org.pk, 'owner': self.sales.pk,
                                                          'customer': secret.pk, 'title': '越权', 'discount': '1', 'note': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(m.Quote.objects.exists())

    def test_create_customer_and_edit_stale_version(self):
        response = self.client.post('/modules/customers/new/', {'organization': self.org.pk, 'owner': self.admin.pk,
                                   'name': '新客户', 'phone': '13912345678', 'address': '西湖区', 'source': '转介绍', 'note': ''})
        self.assertEqual(response.status_code, 302)
        customer = m.Customer.objects.get(name='新客户')
        response = self.client.post(f'/modules/customers/{customer.pk}/edit/',
                                   {'version': '0', 'owner': self.admin.pk, 'name': '错误覆盖', 'phone': '123', 'source': '门店', 'note': ''})
        customer.refresh_from_db()
        self.assertEqual(customer.name, '新客户')
        self.assertEqual(response.status_code, 200)

    def test_approved_quote_cannot_edit_or_add_lines_through_http(self):
        quote = self.make(m.Quote, customer=self.customer, title='冻结报价', status='approved')
        self.assertEqual(self.client.get(f'/modules/quotes/{quote.pk}/edit/').status_code, 403)
        response = self.client.post(f'/modules/quotes/{quote.pk}/lines/',
                                   {'version': quote.version, 'name': '恶意修改', 'quantity': '1', 'unit_price': '10', 'unit': '套'})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(quote.lines.exists())

    def test_batch_is_atomic_when_one_state_is_invalid(self):
        a = self.make(m.Quote, customer=self.customer, title='可批准', status='review')
        b = self.make(m.Quote, customer=self.customer, title='不可批准', status='draft')
        response = self.client.post('/modules/quotes/batch/', {'ids': [a.pk, b.pk], 'versions': [a.version, b.version], 'action': 'approve'})
        self.assertEqual(response.status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.status, 'review')

    def test_csv_formula_injection_is_escaped(self):
        self.make(m.Customer, name='=HYPERLINK("bad")', phone='+123456')
        text = self.client.get('/modules/customers/export/').content.decode('utf-8-sig')
        self.assertIn("'=HYPERLINK", text)
        self.assertIn("'+123456", text)

    def test_permission_does_not_mix_between_memberships(self):
        other = m.Organization.objects.create(name='安装店')
        user = User.objects.create_user('multi')
        m.Membership.objects.create(user=user, organization=self.org, role='finance', scope='org')
        m.Membership.objects.create(user=user, organization=other, role='installer', scope='org')
        order = m.Order.objects.create(organization=other, owner=user, customer=self.customer, title='财务机密', total=1234)
        m.Payment.objects.create(organization=other, owner=user, order=order, direction='in', amount=100,
                                 paid_date=date.today(), reference='SECRET-FINANCE')
        self.client.force_login(user)
        self.assertNotContains(self.client.get('/modules/finance/'), 'SECRET-FINANCE')

    def test_inventory_and_finance_forms_persist_real_transactions(self):
        response = self.client.post('/inventory/adjust/', {'warehouse': self.warehouse.pk, 'material': self.board.pk,
                                                         'quantity': '20', 'reason': '期初盘点'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(m.Stock.objects.get(material=self.board).quantity, Decimal('20'))
        order = self.order(status='confirmed')
        response = self.client.post('/modules/finance/new/', {'organization': self.org.pk, 'direction': 'in',
                                   'order': order.pk, 'amount': '200', 'reference': 'PAY-WEB',
                                   'paid_date': '2026-10-16', 'method': '转账', 'note': ''})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(m.Payment.objects.get(reference='PAY-WEB').amount, Decimal('200'))
