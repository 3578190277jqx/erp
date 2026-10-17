"""Fifth-review sharing, approval and stale-line recovery regressions."""
from datetime import date
from decimal import Decimal
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.models import F
from django.test import override_settings,RequestFactory
from django.test.utils import CaptureQueriesContext
from erp import models as m,services
from erp.services import evidence,collaboration,pricing
from .base import ERPTestCase


class SharingTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        folder=self.enterContext(TemporaryDirectory());self.enterContext(override_settings(MEDIA_ROOT=folder))
        self.factory=m.Organization.objects.create(name='协作工厂',kind='factory')
        self.receiver=User.objects.create_user('factory-receiver')
        m.Membership.objects.create(user=self.receiver,organization=self.factory,role='planner',scope='org')
        m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.order=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.order,self.factory,self.order.version)

    def upload(self,user=None):
        return evidence.upload_attachment(user or self.sales,self.handoff,SimpleUploadedFile('图纸.pdf',b'%PDF-1.7 sharing fixture'),
                 'drawing',share_confirmed=True,parent_version=self.handoff.version,target_id=self.factory.pk)

    def stop(self,user,item,reason='文件含不应共享的信息'):
        return evidence.stop_sharing_attachment(user,item,item.version,self.handoff.version,reason)

    def test_source_manager_can_stop_departed_uploaders_file_without_deleting(self):
        item=self.upload();digest=item.sha256
        self.sales.is_active=False;self.sales.save()
        self.stop(self.manager,item)
        item.refresh_from_db();self.handoff.refresh_from_db()
        self.assertIsNotNone(item.archived_at);self.assertIsNone(item.deleted_at)
        self.assertEqual(item.sha256,digest);self.assertTrue(item.file.storage.exists(item.file.name))
        self.assertEqual(self.handoff.version,2)
        self.assertEqual(evidence.authorized_attachment(self.manager,item.pk).pk,item.pk)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)
        with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.manager,item,item.version)
        self.assertEqual(m.AuditEvent.objects.get(action='stop_sharing_attachment').after['reason'],'文件含不应共享的信息')

    def test_http_stop_after_withdrawal_needs_reason_and_survives_resend(self):
        item=self.upload()
        services.transition(self.manager,self.handoff,'withdraw',self.handoff.version,'暂缓')
        self.handoff.refresh_from_db();self.order.refresh_from_db()
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),f'data-url="/attachments/{item.pk}/stop-sharing/"')
        data={'version':item.version,'parent_version':self.handoff.version}
        response=self.client.post(f'/attachments/{item.pk}/stop-sharing/',data)
        self.assertEqual(response.status_code,302);item.refresh_from_db();self.assertIsNone(item.archived_at)
        response=self.client.post(f'/attachments/{item.pk}/stop-sharing/',data|{'note':'在重发前撤回误传资料'})
        self.assertEqual(response.status_code,302);item.refresh_from_db();self.assertIsNotNone(item.archived_at)
        self.handoff.refresh_from_db()
        collaboration.send_handoff(self.manager,self.order,self.factory,self.order.version,handoff_version=self.handoff.version)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)
        self.assertIsNone(item.deleted_at)

    def test_receiver_cannot_stop_other_uploaders_but_can_stop_own_file(self):
        source=self.upload()
        with self.assertRaises(PermissionDenied):self.stop(self.receiver,source)
        item=self.upload(self.receiver);self.stop(self.receiver,item)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)

    def test_reason_parent_version_item_version_and_repetition_are_checked(self):
        item=self.upload()
        with self.assertRaises(ValidationError):self.stop(self.manager,item,reason=' ')
        with self.assertRaises(ValidationError):evidence.stop_sharing_attachment(self.manager,item,99,self.handoff.version,'旧版本')
        with self.assertRaises(ValidationError):evidence.stop_sharing_attachment(self.manager,item,item.version,99,'旧父单')
        self.stop(self.manager,item);item.refresh_from_db();self.handoff.refresh_from_db()
        with self.assertRaises(ValidationError):self.stop(self.manager,item)
        self.assertEqual(m.AuditEvent.objects.filter(action='stop_sharing_attachment').count(),1)

    def test_archived_file_does_not_reappear_after_same_factory_resend(self):
        item=self.upload();self.stop(self.manager,item);self.handoff.refresh_from_db()
        services.transition(self.manager,self.handoff,'withdraw',self.handoff.version,'暂缓')
        self.handoff.refresh_from_db();self.order.refresh_from_db()
        collaboration.send_handoff(self.manager,self.order,self.factory,self.order.version,handoff_version=self.handoff.version)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)
        self.assertTrue(item.file.storage.exists(item.file.name))

    def test_source_management_still_needs_source_order_scope(self):
        item=self.upload()
        membership=m.Membership.objects.get(user=self.manager);membership.scope='own';membership.save()
        with self.assertRaises(PermissionDenied):self.stop(self.manager,item)
        self.client.force_login(self.receiver)
        response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        self.assertNotContains(response,f'data-url="/attachments/{item.pk}/stop-sharing/"')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),f'data-url="/attachments/{item.pk}/stop-sharing/"')

    def test_attachment_context_queries_do_not_grow_with_each_file(self):
        from erp.views.features import feature_context
        self.upload();request=RequestFactory().get('/');request.user=self.sales
        with CaptureQueriesContext(connection) as one:feature_context(request,self.handoff)
        for i in range(10):m.Attachment.objects.create(organization=self.org,owner=self.sales,object_type='handoffs',object_id=self.handoff.pk,
                   file=f'private/synthetic-{i}.pdf',original_name=f'{i}.pdf',size=1,sha256='a'*64)
        with CaptureQueriesContext(connection) as many:context=feature_context(request,self.handoff)
        self.assertEqual(len(context['attachments']),11)
        self.assertLessEqual(len(many),len(one)+2)

    def test_readers_do_not_take_parent_write_locks(self):
        item=self.upload()
        with patch.object(m.Handoff.objects,'select_for_update',side_effect=AssertionError('GET must not lock')):
            self.assertEqual(len(evidence.visible_attachments(self.receiver,self.handoff)),1)
            self.assertEqual(evidence.authorized_attachment(self.receiver,item.pk).pk,item.pk)

    def test_continuously_changing_parent_is_denied_instead_of_using_stale_scope(self):
        item=self.upload();original=m.Handoff.objects.get
        def changed(*args,**kwargs):
            result=original(*args,**kwargs)
            m.Handoff.objects.filter(pk=result.pk).update(version=F('version')+1)
            return result
        with patch.object(m.Handoff.objects,'get',side_effect=changed):
            with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)


class ApprovalRecoveryTests(ERPTestCase):
    def change(self,order,**values):
        return self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=values.pop('proposed_total',order.total),
                         proposed_due_date=date(2026,12,1),note='评审核对',**values)

    def payload(self,order,**values):
        return dict(organization=self.org.pk,owner=self.sales.pk,order=order.pk,
                    proposed_total=str(order.total),proposed_due_date='2026-12-01',note='订单上下文核对',version=1)|values

    def test_change_batch_has_no_ui_or_post_bypass(self):
        order=self.order(status='confirmed');change=self.change(order)
        self.client.force_login(self.manager)
        self.assertNotContains(self.client.get('/modules/changes/?status=pending'),'批量批准')
        response=self.client.post('/modules/changes/batch/',{'action':'approve','ids':[change.pk],'versions':[change.version]})
        self.assertEqual(response.status_code,403)
        change.refresh_from_db();self.assertEqual(change.status,'pending')

    def test_context_prefill_binds_order_and_organization_on_get_and_post(self):
        a=self.order(status='confirmed',owner=self.sales);a.total=850;a.save()
        b=self.order(status='confirmed',owner=self.sales)
        self.client.force_login(self.sales)
        url=f'/modules/changes/new/?order={a.pk}'
        response=self.client.get(url);form=response.context['form']
        self.assertTrue(form.fields['order'].disabled);self.assertTrue(form.fields['organization'].disabled)
        other=m.Organization.objects.create(name='篡改组织')
        response=self.client.post(url,self.payload(b,organization=other.pk,proposed_total='850'))
        self.assertEqual(response.status_code,302,response.context['form'].errors if response.context else '')
        change=m.ChangeRequest.objects.get();self.assertEqual(change.order_id,a.pk)
        self.assertEqual(change.organization_id,self.org.pk);self.assertEqual(change.proposed_total,850)
        self.assertFalse(m.ChangeRequest.objects.filter(order=b).exists())

    def test_foreign_or_invalid_context_is_not_silently_replaced(self):
        other=m.Organization.objects.create(name='外部组织')
        customer=m.Customer.objects.create(organization=other,owner=self.admin,name='外部',phone='内部')
        order=m.Order.objects.create(organization=other,owner=self.admin,customer=customer,title='外部',total=1000,status='confirmed')
        self.client.force_login(self.sales)
        self.assertEqual(self.client.get(f'/modules/changes/new/?order={order.pk}').status_code,404)
        self.assertEqual(self.client.get('/modules/changes/new/?order=bad').status_code,404)
        form=self.client.get('/modules/changes/new/').context['form']
        self.assertFalse(form.fields['order'].disabled);self.assertIsNone(form.initial['proposed_total'])

    def test_removing_added_line_resets_amount_and_leaves_original_product(self):
        order=self.order(status='confirmed')
        original=m.OrderLine.objects.create(order=order,name='原柜',quantity=1,unit_price=1000,line_total=1000)
        change=self.change(order)
        pricing.add_change_line(self.admin,change,dict(operation='add',name='安装',quantity=1,unit_price=200),change.version)
        change.refresh_from_db();line=change.lines.get();self.assertEqual(change.proposed_total,1200)
        self.client.force_login(self.admin)
        response=self.client.post(f'/features/changes/{change.pk}/remove-change-line/',{'version':change.version,'line_id':line.pk})
        self.assertEqual(response.status_code,302)
        change.refresh_from_db();self.assertEqual(change.proposed_total,1000);self.assertFalse(change.lines.exists())
        self.assertTrue(m.OrderLine.objects.filter(pk=original.pk).exists())
        self.assertTrue(m.AuditEvent.objects.filter(action='remove_change_line').exists())

    def test_multiple_stale_targets_can_be_removed_then_explicitly_rebased(self):
        order=self.order(status='confirmed')
        products=[m.OrderLine.objects.create(order=order,name=f'柜{i}',quantity=1,unit_price=500,line_total=500) for i in range(2)]
        a=self.change(order);b=self.change(order)
        for product in products:
            pricing.add_change_line(self.admin,b,dict(operation='update',order_line=product,unit_price=500),b.version);b.refresh_from_db()
            pricing.add_change_line(self.admin,a,dict(operation='remove',order_line=product),a.version);a.refresh_from_db()
        services.transition(self.manager,a,'approve',a.version)
        old_order_version=b.order_version;ids=list(b.lines.values_list('pk',flat=True))
        pricing.remove_change_line(self.admin,b,ids[0],b.version);b.refresh_from_db()
        self.assertEqual(b.lines.count(),1);self.assertEqual(b.order_version,old_order_version)
        self.client.force_login(self.admin);response=self.client.get(f'/modules/changes/{b.pk}/')
        self.assertContains(response,'拟产品 待核对 条')
        self.assertNotContains(response,'data-action="approve"')
        with self.assertRaises(ValidationError):services.transition(self.manager,b,'approve',b.version)
        pricing.remove_change_line(self.admin,b,ids[1],b.version);b.refresh_from_db()
        order.refresh_from_db();self.assertEqual(b.proposed_total,order.total)
        response=self.client.post(f'/modules/changes/{b.pk}/edit/',self.payload(order,version=b.version,proposed_total=''))
        self.assertEqual(response.status_code,302)
        b.refresh_from_db();services.transition(self.manager,b,'approve',b.version)

    def test_line_removal_checks_belonging_version_role_and_freeze(self):
        order=self.order(status='confirmed');a=self.change(order);b=self.change(order)
        line=m.ChangeLine.objects.create(change=a,operation='add',name='增项',unit_price=100,net_amount=100)
        with self.assertRaises(ValidationError):pricing.remove_change_line(self.admin,b,line.pk,b.version)
        with self.assertRaises(ValidationError):pricing.remove_change_line(self.admin,a,line.pk,99)
        with self.assertRaises(PermissionDenied):pricing.remove_change_line(self.finance,a,line.pk,a.version)
        a.status='approved';a.save()
        with self.assertRaises(ValidationError):pricing.remove_change_line(self.admin,a,line.pk,a.version)
        self.assertTrue(m.ChangeLine.objects.filter(pk=line.pk).exists())

    def test_signed_delta_and_zero_count_remain_distinct_from_unknown(self):
        order=self.order(status='confirmed');line=m.OrderLine.objects.create(order=order,name='原柜',quantity=1,unit_price=1000,line_total=1000)
        increase=self.change(order,proposed_total=1200)
        m.ChangeLine.objects.create(change=increase,operation='add',name='安装',unit_price=200,net_amount=200)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/changes/{increase.pk}/'),'+¥200.00')
        decrease=self.change(order,proposed_total=0)
        m.ChangeLine.objects.create(change=decrease,operation='remove',order_line=line)
        response=self.client.get(f'/modules/changes/{decrease.pk}/')
        self.assertContains(response,'−¥1,000.00');self.assertContains(response,'拟产品 0 条')
