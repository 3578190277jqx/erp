from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from erp import models, permissions


class DomainTests(TestCase):
    def setUp(self):
        self.assertTrue(hasattr(models, 'Organization'), 'Organization model must be implemented')
        self.root = models.Organization.objects.create(name='华东公司', kind='company')
        self.store = models.Organization.objects.create(name='杭州店', kind='store', parent=self.root)
        self.other = models.Organization.objects.create(name='上海店', kind='store')
        self.user = User.objects.create_user('owner')
        self.peer = User.objects.create_user('peer')
        self.member = models.Membership.objects.create(user=self.user, organization=self.root, role='boss', scope='tree')
        self.a = models.Customer.objects.create(organization=self.store, owner=self.user, name='杭州客户', phone='13800000001')
        self.b = models.Customer.objects.create(organization=self.other, owner=self.user, name='上海客户', phone='13800000002')
        self.c = models.Customer.objects.create(organization=self.store, owner=self.peer, name='同事客户', phone='13800000003')

    def test_tree_scope_cannot_see_unrelated_organization(self):
        self.assertEqual(set(permissions.scoped(self.user, models.Customer).values_list('pk', flat=True)), {self.a.pk, self.c.pk})

    def test_own_scope_filters_owners(self):
        self.member.organization = self.store
        self.member.scope = 'own'
        self.member.role = 'sales'
        self.member.save()
        self.assertEqual(list(permissions.scoped(self.user, models.Customer)), [self.a])

    def test_user_without_membership_denied(self):
        self.assertFalse(permissions.scoped(self.peer, models.Customer).exists())
        self.assertFalse(permissions.can(self.peer, 'customers', 'create'))

    def test_installer_cannot_write_finance_or_approve_quote(self):
        self.member.role = 'installer'
        self.member.save()
        self.assertFalse(permissions.can(self.user, 'finance', 'create'))
        self.assertFalse(permissions.can(self.user, 'quotes', 'approve'))
        self.assertTrue(permissions.can(self.user, 'installations', 'transition'))

    def test_organization_cycle_rejected(self):
        self.root.parent = self.store
        with self.assertRaises(ValidationError):
            self.root.full_clean()

    def test_nonfinite_negative_or_zero_material_cost_invalid(self):
        for value in ['NaN', 'Infinity', '-1']:
            with self.subTest(value=value), self.assertRaises(ValidationError):
                models.Material(organization=self.store, owner=self.user, name='板材', unit_cost=Decimal(value)).full_clean()

    def test_warehouse_cannot_use_another_organization_material(self):
        material = models.Material.objects.create(organization=self.other, owner=self.user, name='板材')
        warehouse = models.Warehouse.objects.create(organization=self.store, owner=self.user, name='主仓')
        with self.assertRaises(ValidationError):
            models.Stock(organization=self.store, material=material, warehouse=warehouse, quantity=1).full_clean()
