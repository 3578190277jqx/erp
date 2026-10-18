"""Eighth-review scope alignment, upload lifecycle and audit presentation."""
from tempfile import TemporaryDirectory
from unittest.mock import patch
from datetime import timedelta
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.db import connection, transaction
from django.utils import timezone
from erp import models as m
from erp.services import evidence, collaboration
from erp.services.common import snapshot
from .base import ERPTestCase


class EighthReviewFixture(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=self.enterContext(TemporaryDirectory())))
        self.factory=m.Organization.objects.create(name='八次评审工厂',kind='factory')
        self.receiver=User.objects.create_user('eighth-receiver')
        m.Membership.objects.create(user=self.receiver,organization=self.factory,role='planner',scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.source=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.source,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version)

    def upload(self,user):
        self.handoff.refresh_from_db()
        return evidence.upload_attachment(user,self.handoff,SimpleUploadedFile('八次虚构图纸.pdf',b'%PDF-1.7 fixture'),
             'drawing',True,self.handoff.version,self.factory.pk)

    def status(self,status):
        m.Handoff.objects.filter(pk=self.handoff.pk).update(status=status)
        self.handoff.refresh_from_db()


class SharingAndNavigationTests(EighthReviewFixture):
    def test_audit_links_match_active_organization_destination(self):
        group=m.Organization.objects.create(name='集团',kind='company')
        for org in (self.org,self.factory):org.parent=group;org.save()
        tree=User.objects.create_user('tree-auditor')
        m.Membership.objects.create(user=tree,organization=group,role='manager',scope='tree')
        self.client.force_login(tree)
        event=m.AuditEvent.objects.filter(object_type='handoffs',object_id=self.handoff.pk).first()
        for active,linked in ((self.factory.pk,False),(self.org.pk,True),(None,True)):
            with self.subTest(active=active):
                session=self.client.session;session['organization_id']=active;session.save()
                response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
                self.assertEqual(response.status_code,200)
                url=f'/modules/audit/{event.pk}/'
                if linked:self.assertContains(response,f'href="{url}"')
                else:self.assertNotContains(response,f'href="{url}"')
                self.assertEqual(self.client.get(url).status_code,200 if linked else 404)

    def test_rejected_and_withdrawn_block_all_uploads_until_resend(self):
        for status in ('rejected','withdrawn'):
            self.status(status)
            for user in (self.sales,self.manager,self.receiver,self.admin):
                with self.subTest(status=status,user=user.username):
                    with self.assertRaises(PermissionDenied):self.upload(user)
        self.assertFalse(m.Attachment.objects.exists())

    def test_rejected_source_page_has_resend_guidance_and_no_upload(self):
        self.status('rejected');self.client.force_login(self.manager)
        response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        self.assertContains(response,'如需新增共享，请由源门店重新发送交接')
        self.assertNotContains(response,'type="file"')
        response=self.client.post(f'/attachments/handoffs/{self.handoff.pk}/upload/',{
             'kind':'drawing','share_confirmed':'on','parent_version':self.handoff.version,'target_id':self.factory.pk,
             'file':SimpleUploadedFile('伪造.pdf',b'%PDF-1.7 fixture')})
        self.assertEqual(response.status_code,403)

    def test_resend_and_completed_allow_upload_without_restoring_archived_files(self):
        archived=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.receiver,archived,archived.version,self.handoff.version,'误传')
        self.status('rejected');self.source.refresh_from_db()
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version,
                                               handoff_version=self.handoff.version)
        item=self.upload(self.sales)
        self.assertEqual(evidence.authorized_attachment(self.receiver,item.pk).pk,item.pk)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,archived.pk)
        self.status('completed')
        self.upload(self.receiver);self.upload(self.sales)

    def test_superuser_override_records_real_disabled_or_missing_route(self):
        for mode in ('disabled','missing'):
            if mode=='disabled':self.route.enabled=False;self.route.save()
            else:self.route.delete()
            item=self.upload(self.admin)
            event=m.AuditEvent.objects.filter(action='upload_attachment',after__attachment=item.code).get()
            self.assertIs(event.after.get('route_override'),True)

    def test_enabled_route_and_forged_override_do_not_claim_exception(self):
        self.client.force_login(self.sales)
        response=self.client.post(f'/attachments/handoffs/{self.handoff.pk}/upload/',{
             'kind':'drawing','share_confirmed':'on','parent_version':self.handoff.version,'target_id':self.factory.pk,
             'route_override':'true','file':SimpleUploadedFile('正常.pdf',b'%PDF-1.7 fixture')})
        self.assertEqual(response.status_code,302)
        self.upload(self.admin)
        for event in m.AuditEvent.objects.filter(action='upload_attachment'):
            self.assertFalse(event.after.get('route_override',False))

    def test_lost_permission_panel_is_not_an_empty_collection(self):
        self.client.force_login(self.manager)
        with patch('erp.services.evidence.evidence_permissions',return_value=evidence.EvidencePermissions()):
            response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        self.assertContains(response,'附件访问权限已失效')
        self.assertNotContains(response,'暂无附件')
        self.assertNotContains(response,'type="file"')

    def test_normal_withdrawn_factory_detail_is_404(self):
        self.status('withdrawn');self.client.force_login(self.receiver)
        self.assertEqual(self.client.get(f'/modules/handoffs/{self.handoff.pk}/').status_code,404)

    def test_final_epoch_reread_detects_stop_after_metadata_read(self):
        item=self.upload(self.receiver);original=evidence._same_version
        def stop_before_check(parent):
            evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'读取期间停止共享')
            return original(parent)
        with patch('erp.services.evidence._same_version',side_effect=stop_before_check):
            files,rights,stale=evidence.attachment_context(self.receiver,self.handoff)
        self.assertEqual(files,[]);self.assertFalse(rights.visible);self.assertTrue(stale)


class AuditPresentationTests(EighthReviewFixture):
    def event(self,before,after):
        return self.make(m.AuditEvent,owner=self.sales,object_type='handoffs',object_id=self.handoff.pk,
                         code=self.handoff.code,action='withdraw_handoff',before=before,after=after)

    def page(self,event):
        self.client.force_login(self.manager)
        return self.client.get(f'/modules/audit/{event.pk}/')

    def test_nested_snapshots_have_labels_scoped_names_and_recorded_ids(self):
        self.sales.first_name='八次导购';self.sales.save()
        event=self.event({}, {'handoff':snapshot(self.handoff)})
        response=self.page(event)
        self.assertContains(response,'关联名称为当前可见资料，不代表操作当时的名称')
        self.assertContains(response,'交接快照 / 门店订单')
        self.assertContains(response,self.source.code)
        self.assertContains(response,f'杭州门店（ID: {self.org.pk}）')
        self.assertContains(response,f'八次导购（ID: {self.sales.pk}）')
        self.assertContains(response,'原始记录')
        event.refresh_from_db();self.assertEqual(event.after['handoff']['order'],str(self.source.pk))

    def test_differences_only_include_changed_fields_and_translate_choices(self):
        before=snapshot(self.handoff);after=dict(before,status='withdrawn',note='客户撤回')
        response=self.page(self.event(before,after))
        self.assertIn('audit_differences',response.context)
        rows=response.context['audit_differences']
        self.assertEqual({r['key'] for r in rows},{'交接状态','备注 / 处理结论'})
        status=next(r for r in rows if r['key']=='交接状态')
        self.assertEqual(status['before'],'待工厂接单');self.assertEqual(status['after'],'已撤回')

    def test_empty_null_missing_zero_and_false_are_distinct(self):
        event=self.event({'note':'旧备注','route_override':False,'count':0,'nullable':None},
                         {'note':'','route_override':0,'count':False,'nullable':'','new_value':None})
        response=self.page(event);self.assertIn('audit_differences',response.context)
        rows={r['key']:r for r in response.context['audit_differences']}
        self.assertEqual(rows['备注 / 处理结论']['after'],'未填写')
        self.assertEqual(rows['nullable']['before'],'未记录');self.assertEqual(rows['nullable']['after'],'未填写')
        recorded={r['key']:r for r in response.context['audit_recorded_fields']}
        self.assertEqual(recorded['new_value']['stage'],'变更后')
        self.assertEqual(recorded['new_value']['value'],'未记录')
        self.assertNotIn('new_value',rows)
        self.assertEqual(rows['管理员例外上传']['before'],'否');self.assertEqual(rows['管理员例外上传']['after'],'0')
        self.assertEqual(rows['count']['before'],'0');self.assertEqual(rows['count']['after'],'否')

    def test_unscoped_references_are_not_enriched_and_markup_is_escaped(self):
        other=m.Organization.objects.create(name='不可泄露的别店名称')
        outsider=User.objects.create_user('secret-user',first_name='不可泄露的别店人员')
        m.Membership.objects.create(user=outsider,organization=other,role='sales',scope='org')
        customer=m.Customer.objects.create(organization=other,owner=outsider,name='不可泄露的别店客户',phone='123')
        order=m.Order.objects.create(organization=other,owner=outsider,customer=customer,title='不可泄露的别店订单')
        event=self.event({}, {'handoff':{'organization':str(other.pk),'owner':str(outsider.pk),'order':str(order.pk),
                                         'note':'<script>bad()</script>'}})
        response=self.page(event)
        self.assertNotContains(response,'不可泄露的别店')
        self.assertNotContains(response,'<script>bad()</script>')
        self.assertContains(response,'&lt;script&gt;bad()&lt;/script&gt;')
        self.assertContains(response,'当前名称不可用')

    def test_nested_lists_label_each_item_and_batch_reference_lookups(self):
        event=self.event({}, {'handoffs':[snapshot(self.handoff)]})
        self.client.force_login(self.manager)
        with CaptureQueriesContext(connection) as one:
            first=self.client.get(f'/modules/audit/{event.pk}/')
        self.assertContains(first,'交接快照 / 第1项 / 门店订单')
        copies=[]
        for index in range(12):
            order=self.order()
            copies.append(dict(snapshot(self.handoff),order=str(order.pk)))
        event.after={'handoffs':copies};event.save()
        with CaptureQueriesContext(connection) as many:
            response=self.client.get(f'/modules/audit/{event.pk}/')
        self.assertEqual(response.status_code,200)
        self.assertLessEqual(len(many),len(one)+2)

    def test_identical_snapshots_show_no_difference_and_keep_full_evidence(self):
        payload={'note':'保持原值','custom':{'未知字段':'仍保留'}}
        response=self.page(self.event(payload,payload))
        self.assertContains(response,'本次记录没有字段值变化')
        self.assertContains(response,'仍保留')

    def test_removed_change_line_uses_its_own_snapshot_metadata(self):
        change=self.make(m.ChangeRequest,order=self.source,requested_by=self.sales,order_version=self.source.version,
                         proposed_total=1000,proposed_due_date=self.source.due_date)
        line=m.ChangeLine.objects.create(change=change,operation='remove',name='旧柜体',quantity=2,unit_price=100)
        event=self.make(m.AuditEvent,object_type='changes',object_id=change.pk,code=change.code,
                        action='remove_change_line',before={'change':snapshot(change),'line':snapshot(line)},
                        after={'change':snapshot(change),'recalculation_error':''})
        response=self.page(event)
        self.assertContains(response,'申请明细快照 / 变更类型')
        self.assertContains(response,'减项')
        self.assertContains(response,'申请明细快照 / 变更后计价数量')


class EvidenceOperationsTests(EighthReviewFixture):
    def test_epoch_timestamp_tracks_mutation_without_changing_parent(self):
        version,updated=self.handoff.version,self.handoff.updated_at
        first=timezone.now();second=first+timedelta(seconds=3)
        with patch('erp.services.evidence.timezone.now',return_value=first):item=self.upload(self.sales)
        state=m.EvidenceState.objects.get(object_type='handoffs',object_id=self.handoff.pk)
        self.assertEqual(getattr(state,'updated_at',None),first)
        with patch('erp.services.evidence.timezone.now',return_value=second):
            evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'时间可追查')
        state.refresh_from_db();self.assertEqual(state.updated_at,second);self.assertEqual(state.epoch,2)
        self.handoff.refresh_from_db();self.assertEqual(self.handoff.version,version);self.assertEqual(self.handoff.updated_at,updated)

    def test_failed_mutation_rolls_back_epoch_and_timestamp(self):
        item=self.upload(self.sales)
        state=m.EvidenceState.objects.get(object_type='handoffs',object_id=self.handoff.pk)
        self.assertIsNotNone(getattr(state,'updated_at',None));before=(state.epoch,state.updated_at)
        with patch('erp.services.evidence.audit',side_effect=RuntimeError('rollback fixture')):
            with self.assertRaises(RuntimeError):
                evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'必须回滚')
        state.refresh_from_db();item.refresh_from_db()
        self.assertEqual((state.epoch,state.updated_at),before);self.assertIsNone(item.archived_at)

    def test_evidence_admin_is_superuser_read_only(self):
        self.upload(self.sales);state=m.EvidenceState.objects.get()
        self.client.force_login(self.admin)
        index='/admin/erp/evidencestate/'
        self.assertEqual(self.client.get(index).status_code,200)
        response=self.client.get(f'{index}{state.pk}/change/')
        self.assertEqual(response.status_code,200)
        self.assertNotContains(response,'name="_save"')
        self.assertEqual(self.client.post(f'{index}{state.pk}/change/',{'epoch':999}).status_code,403)
        self.assertEqual(self.client.post(f'{index}{state.pk}/delete/',{'post':'yes'}).status_code,403)
        self.assertEqual(self.client.get(f'{index}add/').status_code,403)
        state.refresh_from_db();self.assertEqual(state.epoch,1)
        self.manager.is_staff=True;self.manager.save();self.client.force_login(self.manager)
        self.assertEqual(self.client.get(index).status_code,403)
