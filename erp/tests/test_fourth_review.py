"""Fourth-review regressions exercise real forms and two-party permissions."""
from datetime import date
from decimal import Decimal
from django.core.exceptions import PermissionDenied,ValidationError
from erp import models as m,services
from .base import ERPTestCase


class ChangeIntentTests(ERPTestCase):
    def payload(self,order,**values):
        return dict(organization=self.org.pk,owner=self.admin.pk,order=order.pk,
                    proposed_due_date='2026-12-15',note='客户仅调整交期',version=1)|values

    def change(self,order,**values):
        return self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=values.pop('proposed_total',order.total),
                         proposed_due_date=date(2026,12,15),note='客户变更核对',**values)

    def test_missing_and_blank_amount_create_preserve_original_with_and_without_products(self):
        self.client.force_login(self.admin)
        for products in (False,True):
            for blank in (False,True):
                with self.subTest(products=products,blank=blank):
                    order=self.order(status='confirmed')
                    if products:m.OrderLine.objects.create(order=order,name='衣柜',quantity=1,unit_price=1000,line_total=1000)
                    data=self.payload(order)
                    if blank:data['proposed_total']=''
                    response=self.client.post('/modules/changes/new/',data)
                    self.assertEqual(response.status_code,302,response.context['form'].errors if response.context else '')
                    change=m.ChangeRequest.objects.filter(order=order).get()
                    self.assertEqual(change.proposed_total,1000)
                    services.transition(self.manager,change,'approve',change.version)
                    order.refresh_from_db();self.assertEqual(order.total,1000)
                    self.assertEqual(order.due_date,date(2026,12,15))

    def test_blank_edit_preserves_current_amount_and_rechecks_order_version(self):
        order=self.order(status='confirmed');change=self.change(order)
        self.client.force_login(self.admin)
        response=self.client.post(f'/modules/changes/{change.pk}/edit/',self.payload(order,proposed_total=''))
        self.assertEqual(response.status_code,302,response.context['form'].errors if response.context else '')
        change.refresh_from_db();self.assertEqual(change.proposed_total,1000)
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertEqual(order.total,1000)

    def test_unknown_basis_date_only_change_does_not_reprice(self):
        order=self.order(status='confirmed',discount_confirmed=False)
        self.client.force_login(self.admin)
        response=self.client.post('/modules/changes/new/',self.payload(order))
        self.assertEqual(response.status_code,302)
        change=m.ChangeRequest.objects.get(order=order);self.assertEqual(change.proposed_total,1000)
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertFalse(order.discount_confirmed);self.assertEqual(order.total,1000)

    def test_explicit_zero_remains_explicit_and_is_not_treated_as_blank(self):
        order=self.order(status='confirmed');self.client.force_login(self.admin)
        response=self.client.post('/modules/changes/new/',self.payload(order,proposed_total='0'))
        self.assertEqual(response.status_code,302)
        change=m.ChangeRequest.objects.get(order=order);self.assertEqual(change.proposed_total,0)
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertEqual(order.total,0)

    def test_derived_amount_is_verified_not_silently_corrected_during_approval(self):
        order=self.order(status='confirmed')
        m.OrderLine.objects.create(order=order,name='衣柜',quantity=1,unit_price=1000,line_total=1000)
        change=self.change(order,proposed_discount=Decimal('.9'),proposed_total=999)
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
        change.refresh_from_db();order.refresh_from_db()
        self.assertEqual(change.proposed_total,999);self.assertEqual(order.total,1000)

    def test_historical_mismatch_and_repricing_effect_visible_before_approval(self):
        order=self.order(status='confirmed');order.total=850;order.discount=Decimal('.9');order.save()
        m.OrderLine.objects.create(order=order,name='历史衣柜',quantity=1,unit_price=1000,line_total=900)
        change=self.change(order,proposed_discount=Decimal('.9'),proposed_total=900)
        self.client.force_login(self.manager);response=self.client.get(f'/modules/changes/{change.pk}/')
        self.assertContains(response,'原合同金额')
        self.assertContains(response,'850.00')
        self.assertContains(response,'900.00')
        self.assertContains(response,'产品快照合计与合同金额不一致')
        self.assertContains(response,'data-confirm-copy=')

    def test_full_restore_shows_original_and_proposed_product_counts(self):
        order=self.order(status='confirmed');change=self.change(order,restore_product_snapshot=True,proposed_total=800)
        m.ChangeLine.objects.create(change=change,operation='add',name='补录柜体',quantity=1,unit_price=800,net_amount=800)
        self.client.force_login(self.manager);response=self.client.get(f'/modules/changes/{change.pk}/')
        self.assertContains(response,'原合同产品 0 条')
        self.assertContains(response,'本次操作 1 条')
        self.assertContains(response,'拟产品 1 条')
        self.assertContains(response,'完整性需人工核对')
