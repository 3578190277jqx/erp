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


class CollaborationOwnershipTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        from django.contrib.auth.models import User
        from erp.services import collaboration
        self.factory=m.Organization.objects.create(name='仅接单工厂',kind='factory')
        self.receiver=User.objects.create_user('factory-only')
        self.membership=m.Membership.objects.create(user=self.receiver,organization=self.factory,role='planner',scope='org')
        m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.source=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.source,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version)

    def upload(self,user,name):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from erp.services import evidence
        return evidence.upload_attachment(user,self.handoff,SimpleUploadedFile(name,b'%PDF-1.7 ownership fixture'),'drawing',
                 share_confirmed=True,parent_version=self.handoff.version,target_id=self.factory.pk)

    def test_both_sides_cannot_delete_each_others_files_but_owner_and_superuser_can(self):
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        from erp.services import evidence
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            source=self.upload(self.sales,'门店图纸.pdf');factory=self.upload(self.receiver,'工厂照片.pdf')
            with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.receiver,source,source.version)
            with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.sales,factory,factory.version)
            with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.manager,source,source.version)
            evidence.delete_attachment(self.sales,source,source.version)
            source.refresh_from_db();self.assertIsNotNone(source.deleted_at)
            evidence.delete_attachment(self.admin,factory,factory.version)
            factory.refresh_from_db();self.assertIsNotNone(factory.deleted_at)

    def test_http_only_own_file_gets_delete_control_and_cross_delete_is_forbidden(self):
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            source=self.upload(self.sales,'门店图纸.pdf');factory=self.upload(self.receiver,'工厂照片.pdf')
            self.client.force_login(self.receiver);response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
            self.assertNotContains(response,f'action="/attachments/{source.pk}/delete/"')
            self.assertContains(response,f'action="/attachments/{factory.pk}/delete/"')
            response=self.client.post(f'/attachments/{source.pk}/delete/',{'version':source.version})
            self.assertEqual(response.status_code,403)
            source.refresh_from_db();self.assertIsNone(source.deleted_at)
            response=self.client.post(f'/attachments/{factory.pk}/delete/',{'version':factory.version})
            self.assertEqual(response.status_code,302)
            factory.refresh_from_db();self.assertIsNotNone(factory.deleted_at)

    def test_uploader_still_needs_current_parent_business_permission(self):
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        from erp.services import evidence
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            item=self.upload(self.receiver,'工厂照片.pdf')
            self.membership.role='engineer';self.membership.save()
            with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.receiver,item,item.version)
            item.refresh_from_db();self.assertIsNone(item.deleted_at)

    def test_role_gate_and_order_scope_both_control_withdrawal(self):
        from unittest.mock import patch
        from erp.permissions import role_allows
        from erp.services import collaboration
        def policy(role,module,action):
            return False if module=='handoffs' and action=='withdraw' else role_allows(role,module,action)
        with patch('erp.permissions.role_allows',side_effect=policy):
            self.assertNotIn('withdraw',dict(collaboration.handoff_actions(self.sales,self.handoff)))
            self.client.force_login(self.sales)
            self.assertNotContains(self.client.get(f'/modules/orders/{self.source.pk}/'),'data-action="withdraw"')
            response=self.client.post(f'/modules/handoffs/{self.handoff.pk}/action/',
                                      {'action':'withdraw','version':self.handoff.version,'note':'角色撤回权限停用'})
            self.assertEqual(response.status_code,403)
        self.handoff.refresh_from_db();self.assertEqual(self.handoff.status,'sent')

    def test_new_factory_order_has_confirmed_noncommercial_basis(self):
        from erp.services import collaboration
        local=collaboration.accept_handoff(self.receiver,self.handoff,self.handoff.version)
        self.assertEqual(local.kind,'manufacturing');self.assertEqual(local.total,0)
        self.assertTrue(local.discount_confirmed)
