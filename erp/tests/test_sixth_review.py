"""Sixth-review evidence lifecycle and approval consistency regressions."""
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from django.test import override_settings
from erp import models as m,services
from erp.services import collaboration,evidence,pricing
from .base import ERPTestCase


class LifecycleTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=self.enterContext(TemporaryDirectory())))
        self.factory=m.Organization.objects.create(name='生命周期验收工厂',kind='factory')
        self.receiver=User.objects.create_user('lifecycle-receiver')
        m.Membership.objects.create(user=self.receiver,organization=self.factory,role='planner',scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.source=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.source,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version)

    def upload(self,user):
        self.handoff.refresh_from_db()
        return evidence.upload_attachment(user,self.handoff,SimpleUploadedFile('生产资料.pdf',b'%PDF-1.7 fictional evidence'),
                  'drawing',True,self.handoff.version,self.factory.pk)

    def test_disabled_route_blocks_all_receiver_writes_but_preserves_read(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        for status in ('sent','accepted','completed'):
            self.handoff.status=status;self.handoff.save(update_fields=['status'])
            with self.subTest(status=status):
                self.assertFalse(evidence.can_write_evidence(self.receiver,self.handoff))
                self.assertEqual(evidence.authorized_attachment(self.receiver,item.pk).pk,item.pk)
                with self.assertRaises(PermissionDenied):self.upload(self.receiver)
                with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.receiver,item,item.version)
                with self.assertRaises(PermissionDenied):evidence.stop_sharing_attachment(self.receiver,item,item.version,self.handoff.version,'撤回')
        item.refresh_from_db();self.assertIsNone(item.archived_at);self.assertIsNone(item.deleted_at)
        self.client.force_login(self.receiver)
        page=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        self.assertFalse(page.context['can_attach'])
        self.assertNotContains(page,'type="file" name="file"');self.assertNotContains(page,'撤回我的文件')

    def test_disabled_route_keeps_source_governance_and_factory_rejection(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'停止误传')
        self.upload(self.sales)
        self.handoff.refresh_from_db()
        with self.assertRaises(PermissionDenied):collaboration.accept_handoff(self.receiver,self.handoff,self.handoff.version)
        collaboration.accept_handoff(self.receiver,self.handoff,self.handoff.version,True,'协作已停用，拒绝在途单')
        self.handoff.refresh_from_db();self.assertEqual(self.handoff.status,'rejected')

    def test_manual_archive_cause_and_role_specific_labels(self):
        item=self.upload(self.sales);self.upload(self.receiver)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),'停止向工厂共享')
        self.client.force_login(self.receiver)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),'撤回我的文件')
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'误传')
        item.refresh_from_db();self.assertEqual(getattr(item,'archive_reason',''),'stopped')
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),'人工停止共享 · 仅源门店查阅')

    def test_reassignment_records_a_distinct_archive_cause(self):
        item=self.upload(self.receiver)
        services.transition(self.manager,self.handoff,'withdraw',self.handoff.version,'改派')
        self.handoff.refresh_from_db();self.source.refresh_from_db()
        target=m.Organization.objects.create(name='新接单厂',kind='factory')
        m.CollaborationRoute.objects.create(source=self.org,target=target)
        collaboration.send_handoff(self.manager,self.source,target,self.source.version,handoff_version=self.handoff.version)
        item.refresh_from_db();self.assertEqual(getattr(item,'archive_reason',''),'reassigned')
        self.assertTrue(item.file.storage.exists(item.file.name));self.assertIsNone(item.deleted_at)

    def test_deletion_advances_parent_version(self):
        item=self.upload(self.sales);old=self.handoff.version
        evidence.delete_attachment(self.sales,item,item.version)
        self.handoff.refresh_from_db();self.assertEqual(self.handoff.version,old+1)
        self.assertTrue(m.AuditEvent.objects.filter(action='delete_attachment').exists())

    def test_deletion_after_metadata_read_denies_download(self):
        item=self.upload(self.sales);same=evidence._same_version;deleted=False
        def delete_before_check(parent):
            nonlocal deleted
            if not deleted:
                deleted=True;evidence.delete_attachment(self.sales,item,item.version)
            return same(parent)
        with patch('erp.services.evidence._same_version',side_effect=delete_before_check):
            with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)

    def test_download_retries_successfully_after_a_harmless_version_change(self):
        item=self.upload(self.sales);same=evidence._same_version;checks=0
        def change_once(parent):
            nonlocal checks
            checks+=1
            if checks==1:m.Handoff.objects.filter(pk=parent.pk).update(version=F('version')+1)
            return same(parent)
        with patch('erp.services.evidence._same_version',side_effect=change_once):
            self.assertEqual(evidence.authorized_attachment(self.receiver,item.pk).pk,item.pk)
        self.assertEqual(checks,2)

    def test_detail_never_mixes_new_parent_version_into_old_page(self):
        self.upload(self.sales)
        m.Handoff.objects.filter(pk=self.handoff.pk).update(version=F('version')+1)
        files,rights,stale=evidence.attachment_context(self.receiver,self.handoff)
        self.assertEqual(files,[]);self.assertFalse(rights.visible);self.assertTrue(stale)

    def test_purge_only_retries_authorized_deletions_not_archived_files(self):
        from io import StringIO
        from django.core.management import call_command
        archived=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,archived,archived.version,self.handoff.version,'误传')
        deleted=self.upload(self.sales)
        evidence.delete_attachment(self.sales,deleted,deleted.version)
        call_command('purge_attachments',stdout=StringIO())
        archived.refresh_from_db();deleted.refresh_from_db()
        self.assertTrue(archived.file.storage.exists(archived.file.name));self.assertIsNone(archived.purged_at)
        self.assertFalse(deleted.file.storage.exists(deleted.file.name));self.assertIsNotNone(deleted.purged_at)


class ApprovalConsistencyTests(ERPTestCase):
    def change(self,order,**values):
        return m.ChangeRequest.objects.create(organization=order.organization,owner=self.sales,requested_by=self.sales,
            order=order,order_version=order.version,proposed_total=values.pop('proposed_total',order.total),
            proposed_due_date=date(2026,12,1),note='六次评审核对',**values)

    def event(self,change,user,action='edit'):
        return m.AuditEvent.objects.create(organization=change.organization,owner=user,object_type='changes',
                  object_id=change.pk,code=change.code,action=action)

    def test_ui_matches_each_server_approval_blocker(self):
        cases=('applicant','editor','factory','version','status','received','plan','plan_sum','handoff','pricing','breakdown')
        factory=m.Organization.objects.create(name='审批工厂',kind='factory')
        m.Membership.objects.create(user=self.manager,organization=factory,role='manager',scope='org')
        for case in cases:
            with self.subTest(case=case):
                order=self.order(status='confirmed');change=self.change(order)
                if case=='applicant':change.requested_by=self.manager
                if case=='editor':self.event(change,self.manager)
                if case=='factory':
                    source=order
                    handoff=self.make(m.Handoff,order=source,target_organization=factory,payload={})
                    customer=m.Customer.objects.create(organization=factory,owner=self.manager,name='工厂客户',phone='内部')
                    order=m.Order.objects.create(organization=factory,owner=self.manager,customer=customer,title='工厂单',kind='manufacturing',
                        source_handoff=handoff,status='confirmed',discount_confirmed=True)
                    change.order=order;change.organization=factory;change.proposed_total=0
                if case=='version':change.order_version=99
                if case=='status':order.status='completed';order.save(update_fields=['status'])
                if case=='received':
                    self.make(m.Payment,order=order,direction='in',amount=800,paid_date=date.today(),reference='SIX-PAID')
                    change.proposed_total=600
                if case in ('plan','plan_sum'):
                    self.make(m.PaymentPlan,order=order,sequence=1,name='测试期',amount=1000 if case=='plan' else 900,
                              due_date=date.today(),gate='start')
                    change.proposed_total=1100;change.revise_payment_plan=case=='plan_sum'
                if case=='handoff':self.make(m.Handoff,order=order,target_organization=factory,payload={})
                if case in ('pricing','breakdown'):
                    m.OrderLine.objects.create(order=order,name='原产品',quantity=1,unit_price=1000,line_total=1000)
                    if case=='pricing':change.proposed_total=1100
                    else:
                        change.proposed_discount=1
                        self.make(m.Breakdown,order=order)
                change.save()
                with self.assertRaises(ValidationError) as blocked:services.transition(self.manager,change,'approve',change.version)
                self.client.force_login(self.manager);response=self.client.get(f'/modules/changes/{change.pk}/')
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.context.get('change_approval_blocker'),'；'.join(blocked.exception.messages))
                self.assertFalse(any(action=='approve' for action,_ in response.context['actions']))
                self.assertContains(response,'暂不可批准')

    def test_valid_approval_preview_is_read_only_and_remains_approvable(self):
        order=self.order(status='confirmed');change=self.change(order)
        self.client.force_login(self.manager);before=m.AuditEvent.objects.count()
        with patch.object(m.Order.objects,'select_for_update',side_effect=AssertionError('GET approval must not lock')):
            response=self.client.get(f'/modules/changes/{change.pk}/')
        self.assertTrue(any(action=='approve' for action,_ in response.context['actions']))
        self.assertEqual(m.AuditEvent.objects.count(),before)
        services.transition(self.manager,change,'approve',change.version)

    def test_tied_audits_cannot_hide_the_last_product_editor(self):
        order=self.order(status='confirmed');change=self.change(order)
        first=self.event(change,self.sales);last=self.event(change,self.manager,'remove_change_line')
        m.AuditEvent.objects.filter(pk__in=[first.pk,last.pk]).update(created_at=last.created_at)
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
        services.transition(self.admin,change,'approve',change.version)

    def test_recalculated_amount_records_current_order_version_without_confirming_it(self):
        order=self.order(status='confirmed');change=self.change(order)
        product=m.OrderLine.objects.create(order=order,name='原产品',quantity=1,unit_price=1000,line_total=1000)
        pricing.add_change_line(self.admin,change,dict(operation='add',name='增项',quantity=1,unit_price=200),change.version)
        change.refresh_from_db();old_version=change.order_version
        order.total=1300;order.version+=1;order.save()
        product.unit_price=1300;product.line_total=1300;product.save()
        pricing.remove_change_line(self.admin,change,change.lines.get().pk,change.version)
        change.refresh_from_db();self.assertEqual(change.order_version,old_version);self.assertEqual(change.proposed_total,1300)
        data=m.AuditEvent.objects.get(action='remove_change_line').after
        self.assertEqual(data.get('recalculated_against_order_version'),order.version)
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)

    def test_failed_recalculation_is_not_recorded_as_a_successful_new_baseline(self):
        order=self.order(status='confirmed');change=self.change(order)
        lines=[m.ChangeLine.objects.create(change=change,operation='update',name='已失效',quantity=1,unit_price=500) for _ in range(2)]
        pricing.remove_change_line(self.admin,change,lines[0].pk,change.version)
        data=m.AuditEvent.objects.get(action='remove_change_line').after
        self.assertIn('recalculated_against_order_version',data)
        self.assertIsNone(data['recalculated_against_order_version'])
        self.assertEqual(data.get('attempted_order_version'),order.version)
        self.assertTrue(data['recalculation_error'])
