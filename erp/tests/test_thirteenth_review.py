"""Thirteenth review: typed legacy evidence and symmetric operation summaries."""
from copy import deepcopy
from unittest.mock import patch
from django.core.exceptions import ValidationError
from erp import models as m, services
from erp.audit_display import audit_context
from erp.services import pricing, evidence
from erp.services.common import snapshot
from .test_eighth_review import EighthReviewFixture


class ThirteenthFixture(EighthReviewFixture):
    def event(self,before,after,**kwargs):
        return self.make(m.AuditEvent,object_type=kwargs.pop('object_type','orders'),
            object_id=kwargs.pop('object_id',self.source.pk),code=kwargs.pop('code',self.source.code),
            action=kwargs.pop('action','test'),before=before,after=after,**kwargs)

    def context(self,event):return audit_context(self.manager,event,self.org.pk)

    def page(self,event,full=False):
        self.client.force_login(self.manager)
        return self.client.get(f'/modules/audit/{event.pk}/'+('?audit_view=full' if full else ''))

    def attachment_payload(self):
        item=self.make(m.Attachment,object_type='orders',object_id=self.source.pk,
            file='private/secret/opaque.pdf',original_name='图纸.pdf',kind='drawing',size=124,sha256='a'*64)
        return snapshot(item)


class LegacyAttachmentTests(ThirteenthFixture):
    def test_legacy_flat_attachment_uses_attachment_fields_and_preserves_original(self):
        before=self.attachment_payload();original=deepcopy(before)
        event=self.event(before,{'attachment':before['code']},action='delete_attachment')
        context=self.context(event)
        rows=context['audit_payloads'][0]['rows'];values={r['key']:r['value'] for r in rows}
        self.assertEqual(values['附件状态快照 / 附件类型'],'图纸/方案')
        self.assertIn('附件状态快照 / 原文件名',values)
        self.assertIn('附件状态快照 / 业务对象 ID',values)
        self.assertNotIn(before['file'],values.values())
        self.assertIn(before['file'],context['audit_raw'])
        for full in (False,True):
            response=self.page(event,full)
            self.assertContains(response,'图纸/方案');self.assertNotContains(response,'订单类型')
        event.refresh_from_db();self.assertEqual(event.before,original)
        self.assertEqual(event.object_type,'orders')

    def test_missing_or_mismatched_legacy_fingerprint_is_not_guessed(self):
        payload=self.attachment_payload()
        variants=[dict(payload,object_id='999'),dict(payload,organization='999'),dict(payload,object_type='changes'),
                  dict(payload,sha256=None),{k:v for k,v in payload.items() if k!='original_name'}]
        for before in variants:
            with self.subTest(payload=before):
                rows=self.context(self.event(before,{},action='delete_attachment'))['audit_payloads'][0]['rows']
                self.assertFalse(any(r['path'][:1]==('attachment_state',) for r in rows))
        rows=self.context(self.event(payload,{},action='edit'))['audit_payloads'][0]['rows']
        self.assertFalse(any(r['path'][:1]==('attachment_state',) for r in rows))

    def test_two_flat_phases_compare_as_attachment_with_technical_version_separate(self):
        before=self.attachment_payload();after=dict(before,kind='photo',version='2')
        context=self.context(self.event(before,after,action='delete_attachment'))
        self.assertEqual(len(context['audit_differences']),1)
        self.assertEqual(context['audit_differences'][0]['key'],'附件状态快照 / 附件类型')
        self.assertEqual(context['audit_differences'][0]['after'],'现场照片')
        self.assertEqual(len(context['audit_technical_differences']),1)

    def test_attachment_normalization_is_pure_and_does_not_read_current_records(self):
        from erp.audit_types import normalize_stage
        before=self.attachment_payload();event=self.event(before,{},action='delete_attachment')
        with self.assertNumQueries(0):
            normalized,note=normalize_stage(event,before)
        self.assertEqual(normalized,{'attachment_state':before});self.assertTrue(note)

    def test_normalized_views_escape_filename_and_keep_reference_and_event_scope(self):
        secret=m.Organization.objects.create(name='不可见上传组织')
        before=dict(self.attachment_payload(),original_name='<script>bad()</script>.pdf',uploaded_from_organization=str(secret.pk))
        event=self.event(before,{},action='delete_attachment')
        for full in (False,True):
            response=self.page(event,full)
            self.assertContains(response,'&lt;script&gt;bad()&lt;/script&gt;.pdf')
            self.assertNotContains(response,'<script>bad()</script>');self.assertNotContains(response,secret.name)
            self.client.force_login(self.receiver)
            self.assertEqual(self.client.get(f'/modules/audit/{event.pk}/'+('?audit_view=full' if full else '')).status_code,404)


class LineOperationTests(ThirteenthFixture):
    def test_quote_zero_and_nonzero_add_delete_are_symmetric_and_not_fourteen_changes(self):
        for price in (0,50):
            quote=self.make(m.Quote,customer=self.customer,title='背板报价')
            quote=services.add_line(self.admin,quote,{'name':'背板','quantity':2,'unit_price':price},quote.version)
            added=m.AuditEvent.objects.get(code=quote.code,action='add_line')
            self.assertIn('新增 1 条明细',self.context(added)['audit_summary'])
            self.assertNotIn('14 个',self.context(added)['audit_summary'])
            self.assertContains(self.page(added),'本条已记录新增的明细')
            self.assertNotContains(self.page(added),'历史载荷结构不同')
            line=quote.lines.get();quote=services.add_line(self.admin,quote,{},quote.version,delete_id=line.pk)
            removed=m.AuditEvent.objects.get(code=quote.code,action='delete_line')
            self.assertIn('移除 1 条明细',self.context(removed)['audit_summary'])
            self.assertContains(self.page(removed),'本条已记录移除的明细')
            self.assertNotContains(self.page(removed),'历史载荷结构不同')
            self.assertEqual(self.context(added)['audit_line_operation']['description'],self.context(removed)['audit_line_operation']['description'])

    def test_material_documents_keep_shared_metadata_and_human_parent_labels(self):
        for model,fields,values,parent_label in [
            (m.Purchase,dict(warehouse=self.warehouse,supplier='供应商'),dict(material=self.board,quantity=2,unit_price=5),'采购订单'),
            (m.Breakdown,dict(order=self.source,quantity=1),dict(material=self.board,quantity=2,waste='0.1'),'拆单任务')]:
            with self.subTest(model=model):
                obj=self.make(model,**fields);obj=services.add_line(self.admin,obj,values,obj.version)
                event=m.AuditEvent.objects.get(code=obj.code,action='add_line');context=self.context(event)
                self.assertIn('新增 1 条明细',context['audit_summary'])
                self.assertIn(self.board.name,context['audit_line_operation']['description'])
                self.assertIn('明细快照 / '+parent_label,{r['key'] for r in context['audit_payloads'][1]['rows']})
                obj=services.add_line(self.admin,obj,{},obj.version,delete_id=obj.lines.get().pk)
                self.assertIn('移除 1 条明细',self.context(m.AuditEvent.objects.get(code=obj.code,action='delete_line'))['audit_summary'])
        quote=self.make(m.Quote,customer=self.customer,title='中文父级')
        services.add_line(self.admin,quote,{'name':'柜体','quantity':1,'unit_price':0},quote.version)
        context=self.context(m.AuditEvent.objects.get(code=quote.code,action='add_line'))
        self.assertIn('明细快照 / 报价单',{r['key'] for r in context['audit_payloads'][1]['rows']})

    def test_change_item_movement_is_request_evidence_not_contract_product_movement(self):
        change=self.make(m.ChangeRequest,order=self.source,order_version=self.source.version,
            proposed_total=self.source.total,proposed_due_date=self.source.due_date,note='加项')
        change=pricing.add_change_line(self.admin,change,dict(operation='add',name='免费加板',quantity=2,unit_price=0),change.version)
        context=self.context(m.AuditEvent.objects.get(action='add_change_line'))
        self.assertIn('新增 1 条申请明细',context['audit_summary'])
        self.assertEqual(self.source.lines.count(),1)
        pricing.remove_change_line(self.admin,change,change.lines.get().pk,change.version)
        context=self.context(m.AuditEvent.objects.get(action='remove_change_line'))
        self.assertIn('移除 1 条申请明细',context['audit_summary'])
        self.assertEqual(self.source.lines.count(),1)

    def test_unknown_malformed_mismatched_and_two_sided_rows_do_not_claim_movements(self):
        row={'id':'1','quote':'1','name':'不能猜'}
        for action,before,after,target in [
            ('unknown',{}, {'line':row},1),('add_line',{}, {'line':dict(row,id='bad')},1),
            ('add_line',{}, {'line':row},999),('add_line',{'line':row},{'line':dict(row,name='改了')},1),
            ('delete_line',{'line':{'name':'无身份'}},{},1)]:
            with self.subTest(action=action,target=target):
                context=self.context(self.event(before,after,action=action,object_type='quotes',object_id=target))
                self.assertFalse(context['audit_line_operation'])
                self.assertNotIn('新增 1 条',context['audit_summary']);self.assertNotIn('移除 1 条',context['audit_summary'])

    def test_line_descriptions_escape_names_and_never_resolve_hidden_material_names(self):
        other=m.Organization.objects.create(name='外部组织')
        secret=m.Material.objects.create(organization=other,owner=self.admin,name='隐藏物料',unit_cost=1)
        row={'id':'1','purchase':str(self.source.pk),'material':str(secret.pk),'quantity':'2','unit_price':'0'}
        event=self.event({'version':'1'},{'version':'2','line':row},action='add_line',object_type='purchases')
        response=self.page(event);self.assertNotContains(response,secret.name)
        self.assertIn('ID:',response.context['audit_line_operation']['description'])
        row={'id':'1','quote':str(self.source.pk),'name':'<script>row()</script>','quantity':'1','unit_price':'0'}
        response=self.page(self.event({'version':'1'},{'version':'2','line':row},action='add_line',object_type='quotes'))
        self.assertNotContains(response,'<script>row()</script>');self.assertContains(response,'&lt;script&gt;row()&lt;/script&gt;')

    def test_shared_specs_do_not_enable_generic_change_request_editing(self):
        change=self.make(m.ChangeRequest,order=self.source,order_version=self.source.version,
            proposed_total=self.source.total,proposed_due_date=self.source.due_date,status='rejected')
        with self.assertRaises(ValidationError):services.add_line(self.admin,change,{},change.version)
        self.assertFalse(change.lines.exists())


class BalancedEvidenceTests(ThirteenthFixture):
    def test_generic_single_sided_summary_counts_before_and_result_without_inferred_changes(self):
        context=self.context(self.event({'note':'同值','old_only':'之前'}, {'note':'同值','new_only':'之后'}))
        self.assertIn('1 个结果字段',context['audit_summary'])
        self.assertIn('1 个操作前字段',context['audit_summary'])
        self.assertEqual(context['audit_differences'],[])
        self.assertFalse(context['audit_line_operation'])

    def test_parent_context_is_excluded_from_counts_but_labeled_in_full_snapshot(self):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'停止原因')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        context=self.context(event)
        expected_before=sum(r['stage']=='变更前' and r['path'][0]!='handoff' for r in context['audit_recorded_fields'])
        self.assertIn(f'{expected_before} 个操作前字段',context['audit_summary'])
        response=self.page(event,True)
        self.assertContains(response,'含操作时父单据上下文')
        self.assertContains(response,'未记录的后值不作推断')
        self.assertTrue(response.context['audit_payloads'][0]['context_note'])

    def test_unknown_handoff_wrapper_keeps_generic_one_sided_full_view(self):
        event=self.event({'handoff':{'note':'只有前值'}},{'policy':'规则'},object_type='handoffs')
        response=self.page(event,True)
        self.assertNotContains(response,'含操作时父单据上下文')
        self.assertIn('1 个操作前字段',response.context['audit_summary'])
