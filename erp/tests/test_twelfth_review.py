"""Twelfth review: row evidence, typed audit subjects and truthful views."""
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch
from django.utils import timezone
from erp import models as m, services
from erp.audit_display import audit_context
from erp.services import pricing, receivables, evidence
from erp.services.common import snapshot, audit
from erp.views.modules import display_value
from .base import ERPTestCase
from .test_eighth_review import EighthReviewFixture


class RowEvidenceTests(ERPTestCase):
    def test_add_and_delete_lines_keep_exact_rows_for_all_three_documents(self):
        quote=self.make(m.Quote,customer=self.customer,title='报价明细证据')
        purchase=self.make(m.Purchase,warehouse=self.warehouse,supplier='供应商')
        breakdown=self.make(m.Breakdown,order=self.order(),quantity=1)
        cases=[(quote,{'name':'零元补板','quantity':'2','unit_price':'0'},'名称'),
               (purchase,{'material':self.board,'quantity':'3','unit_price':'12.34'},'物料'),
               (breakdown,{'material':self.board,'quantity':'4','waste':'0.1'},'物料')]
        for obj,values,label in cases:
            with self.subTest(module=obj.module):
                obj=services.add_line(self.admin,obj,values,obj.version)
                line=obj.lines.get();added=m.AuditEvent.objects.get(code=obj.code,action='add_line')
                self.assertEqual(added.after['line'],snapshot(line))
                self.assertNotIn('line',added.before)
                context=audit_context(self.manager,added,self.org.pk)
                self.assertTrue(any(r['key'].endswith(label) for r in context['audit_recorded_fields']))
                self.assertFalse(context['audit_evidence_warning'])
                expected=snapshot(line)
                services.add_line(self.admin,obj,{},obj.version,delete_id=line.pk)
                removed=m.AuditEvent.objects.get(code=obj.code,action='delete_line')
                self.assertEqual(removed.before['line'],expected)
                self.assertNotIn('line',removed.after)
                self.assertFalse(obj.lines.exists())

    def test_add_change_line_records_identity_quantity_and_price(self):
        order=self.order(status='confirmed')
        m.OrderLine.objects.create(order=order,name='原柜',quantity=1,unit_price=1000,line_total=1000)
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=1000,
                         proposed_due_date=order.due_date,note='追加板件')
        result=pricing.add_change_line(self.admin,change,{'operation':'add','name':'加板','quantity':2,'unit_price':25},change.version)
        event=m.AuditEvent.objects.get(action='add_change_line')
        self.assertEqual(event.after['line'],snapshot(result.lines.get()))
        self.assertEqual(event.after['line']['unit_price'],'25.00')
        self.assertNotIn('line',event.before)

    def test_row_and_parent_version_rollback_if_audit_write_fails(self):
        quote=self.make(m.Quote,customer=self.customer,title='失败回滚')
        with patch('erp.services.lines.audit',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):
                services.add_line(self.admin,quote,{'name':'板件','quantity':1,'unit_price':10},quote.version)
        quote.refresh_from_db();self.assertEqual(quote.version,1);self.assertEqual(quote.total,0)
        self.assertFalse(quote.lines.exists());self.assertFalse(m.AuditEvent.objects.exists())
        services.add_line(self.admin,quote,{'name':'保留板件','quantity':1,'unit_price':10},quote.version)
        quote.refresh_from_db();line=quote.lines.get();version=quote.version
        with patch('erp.services.lines.audit',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):services.add_line(self.admin,quote,{},version,delete_id=line.pk)
        quote.refresh_from_db();self.assertEqual(quote.version,version);self.assertTrue(quote.lines.filter(pk=line.pk).exists())

    def test_historical_missing_rows_and_technical_only_never_claim_business_unchanged(self):
        self.client.force_login(self.manager)
        for action in ('add_line','add_change_line','approved_change','quote_accepted'):
            with self.subTest(action=action):
                event=self.make(m.AuditEvent,object_type='quotes',object_id=1,code='LEGACY',action=action,
                                before={'version':'1'},after={'version':'2'})
                response=self.client.get(f'/modules/audit/{event.pk}/')
                self.assertNotContains(response,'业务字段未变')
                self.assertContains(response,'不能据此认定业务未发生变化')
                self.assertEqual(response.context['audit_heading'],'本次操作记录')
                if action in ('add_line','add_change_line'):
                    self.assertContains(response,'未记录被添加的明细')
                event.refresh_from_db();self.assertEqual(event.after,{'version':'2'})


class PlanAuditTests(ERPTestCase):
    def setUp(self):
        super().setUp();self.order_obj=self.order(status='confirmed')
        self.plans=[self.make(m.PaymentPlan,order=self.order_obj,sequence=i,name=f'期次{i}',amount=amount,
                    due_date=date(2026,10,20),gate=gate) for i,amount,gate in [(1,300,'start'),(2,300,'ship'),(3,400,'complete')]]

    def test_plan_revision_has_distinct_audit_subject_and_nested_metadata(self):
        previous=receivables.plan_rows(self.order_obj)
        self.order_obj.total=Decimal('900');self.order_obj.save(update_fields=['total'])
        receivables.revise_plan(self.admin,self.order_obj,previous)
        events=list(m.AuditEvent.objects.exclude(object_type='orders'))
        self.assertEqual(len(events),3)
        self.assertTrue(all(e.object_type=='payment_plans' for e in events))
        self.assertEqual(m.PaymentPlan.module,'finance')
        for event in events:
            self.assertEqual(display_value(event,'object_type'),'分期收款计划')
            rows=audit_context(self.manager,event,self.org.pk)['audit_differences']
            self.assertEqual(rows[0]['key'],'应收金额')
        context=audit_context(self.manager,m.AuditEvent.objects.get(object_type='orders'),self.org.pk)
        self.assertEqual(len(context['audit_differences']),3)
        self.assertEqual(len(context['audit_technical_differences']),3)
        self.assertTrue(all(r['key'].startswith('分期计划 / PAY-') and r['key'].endswith('应收金额') for r in context['audit_differences']))

    def test_rescheduled_plan_uses_explicit_type_and_due_date_labels(self):
        receivables.reschedule_plan(self.admin,self.order_obj)
        events=m.AuditEvent.objects.filter(action='reschedule_payment_plan')
        self.assertEqual(events.count(),2)
        for event in events:
            self.assertEqual(event.object_type,'payment_plans')
            self.assertEqual(audit_context(self.manager,event,self.org.pk)['audit_differences'][0]['key'],'到期日')

    def test_legacy_plan_disambiguates_using_evidence_not_colliding_payment_id(self):
        plan=self.plans[0];before=snapshot(plan);after=dict(before,amount='250.00',version='2')
        self.make(m.Payment,id=plan.pk,direction='in',order=self.order_obj,amount=100,reference='碰撞流水',paid_date=date(2026,10,19))
        event=self.make(m.AuditEvent,object_type='finance',object_id=plan.pk,code=plan.code,
                        action='revise_payment_plan',before=before,after=after)
        self.assertEqual(display_value(event,'object_type'),'分期收款计划')
        rows=audit_context(self.manager,event,self.org.pk)['audit_payloads'][0]['rows']
        keys={r['key'] for r in rows}
        self.assertIn('期次',keys);self.assertIn('履约前置节点',keys);self.assertIn('合同订单',keys)
        self.assertNotIn('应收订单',keys)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/audit/{event.pk}/'),'分期收款计划')
        event.refresh_from_db();self.assertEqual(event.object_type,'finance');self.assertEqual(event.before,before)

    def test_ambiguous_legacy_shapes_and_real_payment_stay_recorded_type(self):
        plan=self.plans[0];before=snapshot(plan)
        variants=[('edit',before),('revise_payment_plan',{'sequence':'1','gate':'start'}),
                  ('revise_payment_plan',dict(before,id='999')),
                  ('revise_payment_plan',dict(before,gate='unknown')),
                  ('revise_payment_plan',dict(before,direction='income'))]
        for action,payload in variants:
            with self.subTest(action=action,payload=payload):
                event=self.make(m.AuditEvent,object_type='finance',object_id=plan.pk,code=plan.code,
                                action=action,before=payload,after=payload)
                self.assertEqual(display_value(event,'object_type'),'财务对账')
        payment=self.make(m.Payment,direction='in',order=self.order_obj,amount=100,reference='真实流水',paid_date=date(2026,10,19))
        event=audit(self.admin,payment,'create')
        self.assertEqual(event.object_type,'finance')
        self.assertEqual(display_value(event,'object_type'),'财务对账')

    def test_order_alias_is_chinese_and_unknown_version_is_not_hidden(self):
        event=self.make(m.AuditEvent,object_type='orders',object_id=self.order_obj.pk,code=self.order_obj.code,
                        action='change_products',before={'lines':[{'id':'1','order':'1','line_total':'100'}],'custom':{'version':1}},
                        after={'lines':[{'id':'1','order':'1','line_total':'90'}],'custom':{'version':2}})
        context=audit_context(self.manager,event,self.org.pk)
        self.assertIn('产品明细 / 第1项 / 合同订单',{r['key'] for r in context['audit_payloads'][0]['rows']})
        self.assertTrue(any(r['key']=='custom / version' for r in context['audit_differences']))

    def test_legacy_plan_requires_valid_event_identity_and_consistent_both_sides(self):
        plan=self.plans[0];payload=snapshot(plan)
        variants=[(0,dict(payload,id='bad'),dict(payload,id='bad')),
                  (plan.pk,payload,dict(payload,id='999')),
                  (plan.pk,payload,{'sequence':'1','gate':'start'})]
        for object_id,before,after in variants:
            with self.subTest(object_id=object_id,after=after):
                event=self.make(m.AuditEvent,object_type='finance',object_id=object_id,code=plan.code,
                                action='revise_payment_plan',before=before,after=after)
                self.assertEqual(display_value(event,'object_type'),'财务对账')

    def test_malformed_legacy_money_and_blank_codes_stay_generic(self):
        plan=self.plans[0];base=snapshot(plan)
        for amount in (None,True,False,{},[], '', 'bad','NaN','Infinity','-Infinity','-1','1.234','1e15'):
            with self.subTest(amount=amount):
                payload=dict(base,amount=amount)
                event=self.make(m.AuditEvent,object_type='finance',object_id=plan.pk,code=plan.code,
                                action='revise_payment_plan',before=payload,after=payload)
                self.assertEqual(display_value(event,'object_type'),'财务对账')
        for code in ('','   '):
            payload=dict(base,code=code)
            event=self.make(m.AuditEvent,object_type='finance',object_id=plan.pk,code=code,
                            action='revise_payment_plan',before=payload,after=payload)
            self.assertEqual(display_value(event,'object_type'),'财务对账')
        for amount in ('300.00',300,300.0,0.1,'0.00'):
            payload=dict(base,amount=amount)
            event=self.make(m.AuditEvent,object_type='finance',object_id=plan.pk,code=plan.code,
                            action='revise_payment_plan',before=payload,after=payload)
            self.assertEqual(display_value(event,'object_type'),'分期收款计划')


class AuditViewTests(EighthReviewFixture):
    def event(self,before,after,**kwargs):
        return self.make(m.AuditEvent,object_type=kwargs.pop('object_type','handoffs'),object_id=self.handoff.pk,
                         code=self.handoff.code,action=kwargs.pop('action','test'),before=before,after=after,**kwargs)

    def page(self,event,query=''):
        self.client.force_login(self.manager)
        return self.client.get(f'/modules/audit/{event.pk}/{query}')

    def test_complete_chinese_snapshots_are_separate_and_concise_values_stay_single(self):
        event=self.event({'note':'旧值','status':'sent'},{'note':'新值','status':'sent'})
        concise=self.page(event)
        self.assertContains(concise,'查看完整中文快照')
        self.assertContains(concise,'>旧值</pre>',count=1)
        full=self.page(event,'?audit_view=full')
        self.assertContains(full,'完整变更前快照 · 2 个字段')
        self.assertContains(full,'完整变更后快照 · 2 个字段')
        self.assertContains(full,'>旧值</pre>',count=1);self.assertContains(full,'>新值</pre>',count=1)
        self.assertContains(full,self.handoff.get_status_display());self.assertContains(full,'返回变化摘要')
        self.assertNotContains(full,'共同上下文 ·')
        self.assertContains(full,'原始记录 · JSON')

    def test_full_snapshot_escaping_and_scope_match_normal_view(self):
        hidden=m.Organization.objects.create(name='隐藏组织')
        event=self.event({'note':'<script>evil()</script>','target':hidden.pk},{'note':'已变','target':self.factory.pk})
        response=self.page(event,'?audit_view=full')
        self.assertNotContains(response,'<script>evil()</script>')
        self.assertContains(response,'&lt;script&gt;evil()&lt;/script&gt;')
        self.assertNotContains(response,hidden.name)
        for query in ('','?audit_view=full'):
            self.client.force_login(self.receiver)
            self.assertEqual(self.client.get(f'/modules/audit/{event.pk}/{query}').status_code,404)

    def test_known_stop_parent_context_is_separate_without_asserting_after_unchanged(self):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'暂停共享')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        response=self.page(event);context=response.context
        parent=context['audit_context_payloads'][0]
        self.assertTrue(all(r['path'][0]=='handoff' for r in parent['rows']))
        self.assertFalse(any(r['path'][0]=='handoff' for payload in context['audit_recorded_payloads'] for r in payload['rows']))
        self.assertContains(response,'操作时父单据上下文')
        self.assertContains(response,'未记录的后值不作推断')
        self.assertNotContains(response,'仅变更前记录的内容 · 24')
        self.assertIn('handoff',event.before);self.assertNotIn('handoff',event.after)

    def test_other_one_sided_wrappers_stay_evidence_not_claimed_unchanged(self):
        event=self.event({'handoff':{'note':'前值'},'attachment_state':{'name':'文件'}},{'reason':'结果'},action='unknown')
        context=audit_context(self.manager,event,self.org.pk)
        self.assertEqual(context['audit_context_payloads'],[])
        self.assertEqual(len(context['audit_recorded_fields']),3)

    def test_audit_header_and_info_use_same_detailed_time_ordinary_header_stays_compact(self):
        event=self.event({}, {'note':'时间'})
        moment=datetime(2026,10,19,0,1,2,tzinfo=dt_timezone.utc)
        m.AuditEvent.objects.filter(pk=event.pk).update(created_at=moment)
        m.Handoff.objects.filter(pk=self.handoff.pk).update(created_at=moment)
        with timezone.override('Asia/Shanghai'):
            response=self.page(event)
            self.assertEqual(response.context['detail_timestamp'],'2026-10-19 08:01:02+08:00')
            self.assertContains(response,'2026-10-19 08:01:02+08:00',count=2)
            ordinary=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
            self.assertEqual(ordinary.context['detail_timestamp'],'2026-10-19 08:01')
