"""Fourteenth-review evidence normalization must never complete missing history."""
from copy import deepcopy
from unittest.mock import patch
from types import SimpleNamespace
from django.core.exceptions import ValidationError
from erp import models as m, services
from erp.forms import line_form
from erp.line_metadata import LINE_SPECS
from erp.audit_types import normalize_stage
from erp.services import evidence
from erp.services.common import snapshot
from .test_thirteenth_review import ThirteenthFixture


class AttachmentPairTests(ThirteenthFixture):
    def stop_event(self,explicit_null=False):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'明确记录的原因')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        before=dict(event.before['attachment_state'])
        if not explicit_null:before.pop('archived_at',None)
        before.pop('archive_reason',None)
        event.before={'handoff':event.before['handoff'],'attachment':before}
        event.after={'attachment':item.code,'sha256':item.sha256,
            'archived_at':event.after['attachment_state']['archived_at'],
            'reason':'明确记录的原因','policy':'保留文件，仅供授权源店查阅。'}
        event.save(update_fields=['before','after'])
        return event

    def test_partial_stop_aligns_only_recorded_facts_and_reports_explicit_action(self):
        event=self.stop_event();original=deepcopy((event.before,event.after))
        context=self.context(event)
        self.assertEqual(context['audit_differences'],[])
        self.assertEqual(context['audit_attachment_operation']['title'],'已记录停止共享')
        self.assertIn('已记录停止共享',context['audit_summary'])
        self.assertEqual(context['audit_operation_notes'][0]['value'],event.after['policy'])
        shared={r['key']:r['value'] for r in context['audit_shared_rows']}
        self.assertEqual(shared['附件状态快照 / 文件校验和'],event.after['sha256'])
        after=context['audit_payloads'][1]['rows']
        self.assertFalse(any(r['path']==('attachment_state','archive_reason') for r in after))
        before=context['audit_payloads'][0]['rows']
        self.assertFalse(any(r['path']==('attachment_state','archived_at') for r in before))
        self.assertIn('2 个结果字段',context['audit_summary'])
        event.refresh_from_db();self.assertEqual((event.before,event.after),original)

    def test_explicit_null_compares_time_but_does_not_invent_archive_category(self):
        context=self.context(self.stop_event(explicit_null=True))
        self.assertEqual([r['key'] for r in context['audit_differences']],['附件状态快照 / 归档时间'])
        self.assertEqual(context['audit_differences'][0]['before'],'尚未归档')
        self.assertFalse(any('归档原因类别' in r['key'] for r in context['audit_differences']))

    def test_delete_patch_aligns_identity_and_hash_without_synthetic_before_time(self):
        before=self.attachment_payload()
        event=self.event(before,{'attachment':before['code'],'sha256':before['sha256'],
            'deleted_at':'2026-10-16T10:17:05Z'},action='delete_attachment')
        context=self.context(event)
        self.assertEqual(context['audit_attachment_operation']['title'],'已记录删除附件')
        self.assertEqual(context['audit_differences'],[])
        self.assertIn('1 个结果字段',context['audit_summary'])
        self.assertEqual(len(context['audit_shared_rows']),2)
        self.assertIn(before['file'],context['audit_raw'])
        self.assertNotIn(before['file'],[r['value'] for r in context['audit_payloads'][0]['rows']])

    def test_mismatches_malformed_times_and_conflicting_wrappers_remain_generic(self):
        event=self.stop_event();before=deepcopy(event.before);after=deepcopy(event.after)
        cases=[]
        for key,value in [('attachment','OTHER'),('sha256','b'*64),('sha256',None),
                          ('archived_at','bad'),('archived_at','2026-11-03T10:00:00'),
                          ('archived_at',True),('archived_at',None),('archived_at',{}),
                          ('archived_at','2026-99-99T10:00:00Z')]:
            cases.append((before,dict(after,**{key:value})))
        for key in ('organization','object_id','code'):
            bad=deepcopy(before);bad['attachment'][key]='999';cases.append((bad,after))
        for key in ('id','organization','code'):
            bad=deepcopy(before);bad['handoff'][key]='999';cases.append((bad,after))
        cases.append((dict(before,attachment_state=before['attachment']),after))
        for left,right in cases:
            with self.subTest(left=left,right=right):
                event.before=deepcopy(left);event.after=deepcopy(right)
                context=self.context(event)
                self.assertFalse(context.get('audit_attachment_operation'))
                self.assertNotIn('已记录停止共享',context['audit_summary'])
                self.assertFalse(context.get('audit_operation_notes'))

    def test_current_producer_keeps_real_state_differences_and_separates_policy(self):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'误传')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        context=self.context(event)
        self.assertEqual(len(context['audit_differences']),2)
        self.assertEqual(len(context['audit_technical_differences']),1)
        self.assertIn('1 个结果字段',context['audit_summary'])
        self.assertNotIn('3 个结果字段',context['audit_summary'])
        self.assertEqual(len(context['audit_operation_notes']),1)
        self.assertTrue(any(r['key']=='附件状态快照 / 单据编号' for r in context['audit_shared_rows']))

    def test_policy_for_unknown_action_stays_generic_recorded_evidence(self):
        event=self.event({'note':'之前'},{'policy':'不能静默隐藏'},action='unknown')
        context=self.context(event)
        self.assertFalse(context.get('audit_operation_notes'))
        self.assertIn('1 个结果字段',context['audit_summary'])
        self.assertTrue(any(r['path']==('policy',) for r in context['audit_recorded_fields']))

    def test_both_http_views_escape_notes_keep_scope_and_describe_payload_coverage(self):
        event=self.stop_event();event.after['policy']='<script>policy()</script>'
        event.save(update_fields=['after'])
        for full in (False,True):
            response=self.page(event,full)
            self.assertContains(response,'&lt;script&gt;policy()&lt;/script&gt;')
            self.assertNotContains(response,'<script>policy()</script>')
            self.assertNotContains(response,'已含完整变更后快照')
            self.assertContains(response,'已记录停止共享')
            if full:self.assertContains(response,'变更后阶段载荷 ·')
            self.client.force_login(self.receiver)
            self.assertEqual(self.client.get(f'/modules/audit/{event.pk}/'+('?audit_view=full' if full else '')).status_code,404)
        simple=self.event({'note':'前'},{'reason':'后'})
        response=self.page(simple)
        self.assertContains(response,'本阶段载荷全部字段')
        self.assertNotContains(response,'已含完整变更前快照')

    def test_pair_projection_is_pure_and_never_queries_current_attachment(self):
        from erp.audit_types import normalize_attachment_transition
        event=self.stop_event();before=deepcopy(event.before);after=deepcopy(event.after)
        with self.assertNumQueries(0):
            left,right,operation=normalize_attachment_transition(event,before,after)
        self.assertTrue(operation)
        self.assertIn('attachment_state',left);self.assertIn('attachment_state',right)
        self.assertEqual(before,event.before);self.assertEqual(after,event.after)

    def test_attachment_kinds_follow_model_choices_and_copy_does_not_guess_age(self):
        payload=self.attachment_payload();event=self.event(payload,{},action='delete_attachment')
        field=m.Attachment._meta.get_field('kind')
        with patch.object(field,'_choices',[*field.choices,('acceptance','验收单')]):
            normalized,note=normalize_stage(event,dict(payload,kind='acceptance'))
        self.assertIn('attachment_state',normalized)
        self.assertTrue(note);self.assertNotIn('旧',note)

    def test_malformed_attachment_kind_remains_untyped_instead_of_raising(self):
        payload=self.attachment_payload();event=self.event(payload,{},action='delete_attachment')
        for kind in ({},[],True,None):
            with self.subTest(kind=kind):
                normalized,note=normalize_stage(event,dict(payload,kind=kind))
                self.assertNotIn('attachment_state',normalized);self.assertEqual(note,'')


class LineEditorSchemaTests(ThirteenthFixture):
    def test_real_forms_keep_fields_cost_hiding_and_material_scope(self):
        quote=self.make(m.Quote,customer=self.customer,title='统一表单')
        purchase=self.make(m.Purchase,warehouse=self.warehouse,supplier='材料商')
        breakdown=self.make(m.Breakdown,order=self.source,quantity=1)
        for document in (quote,purchase,breakdown):
            form=line_form(self.admin,document)
            self.assertEqual(set(form.fields),set(LINE_SPECS[type(document)].fields)|{'version'})
            self.assertIsInstance(LINE_SPECS[type(document)].fields,tuple)
        self.assertNotIn('unit_cost',line_form(self.sales,quote).fields)
        secret=m.Material.objects.create(organization=self.factory,owner=self.admin,name='外部物料',unit_cost=1)
        for document in (purchase,breakdown):
            form=line_form(self.admin,document)
            self.assertFalse(form.fields['material'].queryset.filter(pk=secret.pk).exists())
            self.assertTrue(form.fields['material'].queryset.filter(pk=self.board.pk).exists())

    def test_unsupported_and_dedicated_documents_do_not_get_purchase_forms(self):
        change=self.make(m.ChangeRequest,order=self.source,order_version=self.source.version,
            proposed_total=self.source.total,proposed_due_date=self.source.due_date)
        for document in (change,self.source):
            with self.subTest(model=type(document)):
                with self.assertRaises(ValidationError):line_form(self.admin,document)

    def narrowed_spec(self):
        return SimpleNamespace(model=m.QuoteLine,parent_field='quote',generic_editor=True,
            fields=('name','quantity','unit_price','unit','width_mm','height_mm'))

    def test_form_uses_registered_fields_instead_of_its_own_if_else_schema(self):
        quote=self.make(m.Quote,customer=self.customer,title='字段收窄')
        spec=self.narrowed_spec()
        with patch('erp.forms.LINE_SPECS',{m.Quote:spec},create=True):
            form=line_form(self.admin,quote)
        self.assertEqual(set(form.fields),set(spec.fields)|{'version'})

    def test_writer_enforces_same_registered_fields(self):
        quote=self.make(m.Quote,customer=self.customer,title='写入边界')
        with patch('erp.services.lines.LINE_SPECS',{m.Quote:self.narrowed_spec()}):
            with self.assertRaises(ValidationError):
                services.add_line(self.admin,quote,dict(name='背板',quantity=1,unit_price=0,room='未注册'),quote.version)
        self.assertFalse(quote.lines.exists())
