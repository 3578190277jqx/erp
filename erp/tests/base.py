from datetime import date
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from erp import models as m


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ERPTestCase(TestCase):
    def setUp(self):
        self.org = m.Organization.objects.create(name='杭州门店')
        self.admin = User.objects.create_superuser('admin', password='test-pass-2026')
        self.sales = User.objects.create_user('sales', password='test-pass-2026')
        self.manager = User.objects.create_user('manager', password='test-pass-2026')
        self.finance = User.objects.create_user('finance', password='test-pass-2026')
        self.buyer = User.objects.create_user('buyer', password='test-pass-2026')
        for user, role in [(self.sales, 'sales'), (self.manager, 'manager'), (self.finance, 'finance'), (self.buyer, 'buyer')]:
            m.Membership.objects.create(user=user, organization=self.org, role=role, scope='org')
        self.customer = self.make(m.Customer, name='林女士', phone='13800138000')
        self.warehouse = self.make(m.Warehouse, name='杭州仓')
        self.board = self.make(m.Material, name='白橡木板', unit_cost=100)
        self.hinge = self.make(m.Material, name='缓冲铰链', unit='件', unit_cost=10)

    def make(self, model, **kwargs):
        owner = kwargs.pop('owner', self.admin)
        return model.objects.create(organization=self.org, owner=owner, **kwargs)

    def order(self, **kwargs):
        return self.make(m.Order, customer=self.customer, title='全屋定制', total=1000,
                         contract_number='HT-2026-001', due_date=date(2026, 11, 15), **kwargs)
