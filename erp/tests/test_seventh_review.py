"""Seventh-review business/evidence boundaries and scoped governance."""
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.test import RequestFactory
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.utils import timezone
from erp import models as m,services
from erp.services import evidence,collaboration,pricing
from .base import ERPTestCase


class EvidenceBoundaryTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=self.enterContext(TemporaryDirectory())))
        self.factory=m.Organization.objects.create(name='七次评审工厂',kind='factory')
        self.receiver=User.objects.create_user('epoch-receiver')
        self.factory_manager=User.objects.create_user('epoch-factory-manager')
        for user,role in ((self.receiver,'planner'),(self.factory_manager,'manager')):
            m.Membership.objects.create(user=user,organization=self.factory,role=role,scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.source=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.source,name='原产品',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version)

    def upload(self,user,parent=None):
        parent=parent or self.handoff;parent.refresh_from_db()
        return evidence.upload_attachment(user,parent,SimpleUploadedFile('虚构图纸.pdf',b'%PDF-1.7 synthetic epoch fixture'),
             'drawing',True,parent.version,self.factory.pk)

    def stop(self,user,item):
        self.handoff.refresh_from_db()
        return evidence.stop_sharing_attachment(user,item,item.version,self.handoff.version,'七次评审止损')

    def test_deleting_contract_evidence_does_not_invalidate_pending_approval(self):
        order=self.order(status='confirmed',owner=self.sales)
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,requested_by=self.sales,
                         proposed_total=1000,proposed_due_date=date(2026,12,1),note='仅改交期')
        item=self.upload(self.admin,order);order.refresh_from_db();version=order.version;at=order.updated_at
        evidence.delete_attachment(self.admin,item,item.version)
        order.refresh_from_db();self.assertEqual(order.version,version);self.assertEqual(order.updated_at,at)
        services.transition(self.manager,change,'approve',change.version)

    def test_attachment_mutations_never_full_clean_or_save_parent(self):
        item=self.upload(self.sales,self.source)
        with patch.object(m.Order,'full_clean',wraps=self.source.full_clean) as clean,patch.object(m.Order,'save',wraps=self.source.save) as save:
            evidence.delete_attachment(self.sales,item,item.version)
        self.assertEqual(clean.call_count,0);self.assertEqual(save.call_count,0)

    def test_manual_stop_only_changes_evidence_not_handoff_business_version(self):
        item=self.upload(self.receiver);version=self.handoff.version;at=self.handoff.updated_at
        self.stop(self.manager,item);self.handoff.refresh_from_db()
        self.assertEqual(self.handoff.version,version);self.assertEqual(self.handoff.updated_at,at)

    def test_each_collection_mutation_advances_an_independent_epoch(self):
        epoch=getattr(evidence,'evidence_epoch',lambda parent:0)
        before=epoch(self.handoff);item=self.upload(self.sales)
        self.assertEqual(epoch(self.handoff),before+1)
        self.stop(self.manager,item);self.assertEqual(epoch(self.handoff),before+2)
        item.refresh_from_db();evidence.delete_attachment(self.sales,item,item.version)
        self.assertEqual(epoch(self.handoff),before+3)

    def test_disabled_route_blocks_source_and_factory_upload_but_keeps_source_governance(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        for user in (self.sales,self.manager,self.receiver,self.factory_manager):
            with self.subTest(user=user.username):
                with self.assertRaises(PermissionDenied):self.upload(user)
        self.stop(self.manager,item)
        self.assertEqual(evidence.authorized_attachment(self.manager,item.pk).pk,item.pk)

    def test_factory_uploader_can_stop_own_file_even_after_route_disabled(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        self.stop(self.receiver,item);item.refresh_from_db();self.assertIsNotNone(item.archived_at)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)

    def test_factory_management_can_only_stop_proven_current_factory_uploads(self):
        source=self.upload(self.sales);factory=self.upload(self.receiver)
        self.assertEqual(getattr(source,'uploaded_from_organization_id',None),self.org.pk)
        self.assertEqual(getattr(factory,'uploaded_from_organization_id',None),self.factory.pk)
        self.route.enabled=False;self.route.save()
        with self.assertRaises(PermissionDenied):self.stop(self.factory_manager,source)
        self.stop(self.factory_manager,factory)
        with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.factory_manager,source,source.version)

    def test_factory_management_cannot_borrow_another_roles_scope(self):
        item=self.upload(self.receiver)
        member=m.Membership.objects.get(user=self.factory_manager);member.scope='own';member.save()
        m.Membership.objects.create(user=self.factory_manager,organization=self.factory,role='planner',scope='org')
        with self.assertRaises(PermissionDenied):self.stop(self.factory_manager,item)
        own=self.upload(self.factory_manager);self.route.enabled=False;self.route.save()
        self.stop(self.factory_manager,own)

    def test_legacy_unknown_origin_does_not_give_factory_managers_governance(self):
        item=self.make(m.Attachment,owner=self.receiver,object_type='handoffs',object_id=self.handoff.pk,
                 file='private/unknown.pdf',original_name='历史来源未分类.pdf',size=1,sha256='a'*64)
        with self.assertRaises(PermissionDenied):self.stop(self.factory_manager,item)
        self.route.enabled=False;self.route.save()
        self.stop(self.receiver,item)

    def test_real_business_change_still_invalidates_approval(self):
        order=self.order(status='confirmed')
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,requested_by=self.sales,
                         proposed_total=1000,proposed_due_date=date(2026,12,1),note='待核对')
        order.version+=1;order.save()
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)

    def test_reading_legacy_evidence_does_not_create_epoch_state(self):
        item=self.make(m.Attachment,object_type='handoffs',object_id=self.handoff.pk,file='private/legacy.pdf',
                     original_name='历史文件.pdf',size=1,sha256='d'*64)
        self.assertEqual(m.EvidenceState.objects.count(),0)
        evidence.attachment_context(self.manager,self.handoff);evidence.authorized_attachment(self.manager,item.pk)
        self.assertEqual(m.EvidenceState.objects.count(),0)

    def test_upload_origin_cannot_be_supplied_by_post(self):
        self.client.force_login(self.sales)
        response=self.client.post(f'/attachments/handoffs/{self.handoff.pk}/upload/',{
             'kind':'drawing','share_confirmed':'on','parent_version':self.handoff.version,'target_id':self.factory.pk,
             'uploaded_from_organization':self.factory.pk,'file':SimpleUploadedFile('来源篡改.pdf',b'%PDF-1.7 fixture')})
        self.assertEqual(response.status_code,302)
        item=m.Attachment.objects.get();self.assertEqual(item.uploaded_from_organization_id,self.org.pk)
        self.assertEqual(item.owner_id,self.sales.pk)

    def test_factory_governance_uses_recorded_origin_after_uploader_leaves(self):
        item=self.upload(self.receiver)
        m.Membership.objects.filter(user=self.receiver).delete()
        other=m.Organization.objects.create(name='上传者新雇主',kind='factory')
        m.Membership.objects.create(user=self.receiver,organization=other,role='planner',scope='org')
        self.stop(self.factory_manager,item)
        item.refresh_from_db();self.assertEqual(item.uploaded_from_organization_id,self.factory.pk)
        self.assertIsNotNone(item.archived_at)


class AuditAndPricingTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.order_obj=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.order_obj,name='柜体',quantity=1,unit_price=1000,line_total=1000)

    def change(self):
        change=self.make(m.ChangeRequest,order=self.order_obj,owner=self.sales,requested_by=self.sales,
                        order_version=self.order_obj.version,proposed_total=1000,proposed_due_date=date(2026,12,1),note='审计与预检')
        pricing.add_change_line(self.admin,change,dict(operation='add',name='增项',quantity=1,unit_price=200),change.version)
        change.refresh_from_db();return change

    def archived_fixture(self):
        factory=m.Organization.objects.create(name='审计隔离工厂',kind='factory')
        user=User.objects.create_user('audit-factory-manager')
        m.Membership.objects.create(user=user,organization=factory,role='manager',scope='org')
        handoff=self.make(m.Handoff,owner=self.sales,order=self.order_obj,target_organization=factory,payload={})
        item=self.make(m.Attachment,owner=user,object_type='handoffs',object_id=handoff.pk,file='private/audit-synthetic.pdf',
                 original_name='停止共享证据.pdf',size=1,sha256='b'*64,archived_at=timezone.now(),archive_reason='stopped',
                 uploaded_from_organization=factory)
        reason='仅源店可见的止损原因 <script>alert(1)</script>'
        event=self.make(m.AuditEvent,owner=self.sales,object_type='handoffs',object_id=handoff.pk,code=handoff.code,
                 action='stop_sharing_attachment',after={'attachment':item.code,'reason':reason,'recalculated_against_order_version':3})
        return handoff,item,event,user,reason

    def test_audit_payloads_are_already_scoped_and_visible_in_existing_detail(self):
        _,_,event,user,reason=self.archived_fixture()
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/audit/{event.pk}/'),'仅源店可见的止损原因')
        self.client.force_login(user)
        self.assertEqual(self.client.get(f'/modules/audit/{event.pk}/').status_code,404)

    def test_source_sees_stop_reason_and_authorized_audit_link_but_factory_does_not(self):
        handoff,_,event,user,reason=self.archived_fixture()
        self.client.force_login(self.manager);response=self.client.get(f'/modules/handoffs/{handoff.pk}/')
        self.assertContains(response,'仅源店可见的止损原因')
        self.assertContains(response,f'href="/modules/audit/{event.pk}/"')
        self.assertNotContains(response,'<script>alert(1)</script>')
        self.client.force_login(self.sales);response=self.client.get(f'/modules/handoffs/{handoff.pk}/')
        self.assertContains(response,'仅源店可见的止损原因')
        self.assertNotContains(response,f'href="/modules/audit/{event.pk}/"')
        self.client.force_login(user);response=self.client.get(f'/modules/handoffs/{handoff.pk}/')
        self.assertNotContains(response,'仅源店可见的止损原因')
        self.assertNotContains(response,f'href="/modules/audit/{event.pk}/"')

    def test_audit_payload_has_structured_safe_labels(self):
        _,_,event,_,reason=self.archived_fixture();self.client.force_login(self.manager)
        response=self.client.get(f'/modules/audit/{event.pk}/')
        self.assertContains(response,'停止原因');self.assertContains(response,'拟额计算订单版本')
        self.assertContains(response,'<pre',count=None);self.assertNotContains(response,'<script>alert(1)</script>')

    def test_attachment_reason_queries_do_not_grow_per_file(self):
        from erp.views.features import feature_context
        handoff,item,event,_,reason=self.archived_fixture();request=RequestFactory().get('/');request.user=self.manager
        with CaptureQueriesContext(connection) as one:feature_context(request,handoff)
        for i in range(10):
            extra=self.make(m.Attachment,object_type='handoffs',object_id=handoff.pk,file=f'private/reason-{i}.pdf',
                   original_name=f'虚构{i}.pdf',size=1,sha256='c'*64,archived_at=timezone.now(),archive_reason='stopped')
            self.make(m.AuditEvent,object_type='handoffs',object_id=handoff.pk,code=handoff.code,action='stop_sharing_attachment',
                      after={'attachment':extra.code,'reason':f'批量止损{i}'})
        with CaptureQueriesContext(connection) as many:context=feature_context(request,handoff)
        self.assertLessEqual(len(many),len(one)+2)
        self.assertTrue(all(getattr(item,'stop_reason','') for item in context['attachments']))

    def test_get_calculates_proposed_products_once_and_bounds_source_queries(self):
        change=self.change();self.client.force_login(self.manager)
        with CaptureQueriesContext(connection) as queries,patch('erp.services.pricing.proposed_lines',wraps=pricing.proposed_lines) as calculate:
            response=self.client.get(f'/modules/changes/{change.pk}/')
        self.assertEqual(response.status_code,200);self.assertEqual(calculate.call_count,1)
        source_queries=[q for q in queries if 'FROM "erp_orderline"' in q['sql']]
        self.assertLessEqual(len(source_queries),3)

    def test_post_does_not_accept_a_previous_preview_as_authority(self):
        change=self.change();self.client.force_login(self.manager)
        self.client.get(f'/modules/changes/{change.pk}/')
        # Simulate changed underlying data without a request-local preview token.
        change.lines.update(unit_price=300,net_amount=300)
        self.client.post(f'/modules/changes/{change.pk}/action/',{'action':'approve','version':change.version,
                 'pricing_snapshot':'previous-preview-was-valid','proposed_total':'1200'})
        change.refresh_from_db();self.order_obj.refresh_from_db()
        self.assertEqual(change.status,'pending');self.assertEqual(self.order_obj.total,1000)

    def test_preview_snapshot_cannot_be_reused_for_another_version(self):
        from erp.services.workflows import validate_change_approval
        change=self.change();review=pricing.snapshot_change(change)
        change.version+=1
        with self.assertRaisesMessage(ValidationError,'计价快照'):
            validate_change_approval(self.manager,change,pricing_snapshot=review)
