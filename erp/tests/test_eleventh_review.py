"""Reliable identities, audit meaning, export boundaries and nonduplicated facts."""
import csv
import io
from datetime import datetime, timezone as datetime_timezone
from django.utils import timezone
from erp import models as m, services
from erp.audit_display import audit_context
from erp.services import evidence, pricing
from erp.templatetags.ui import action_name
from .test_eighth_review import EighthReviewFixture


class EleventhFixture(EighthReviewFixture):
    def event(self,before,after,object_type='handoffs',action='test'):
        return self.make(m.AuditEvent,object_type=object_type,object_id=self.handoff.pk,
                         code=self.handoff.code,action=action,before=before,after=after)

    def context(self,event):return audit_context(self.manager,event,self.org.pk)

    def page(self,event):
        self.client.force_login(self.manager)
        return self.client.get(f'/modules/audit/{event.pk}/')


class AuditIdentityTests(EleventhFixture):
    def test_real_product_update_compares_identified_fields(self):
        order=self.order(status='confirmed')
        line=m.OrderLine.objects.create(order=order,name='原柜',quantity=1,unit_price=1000,line_total=1000)
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,
                         proposed_total=1000,proposed_due_date=order.due_date)
        change=pricing.add_change_line(self.admin,change,dict(operation='update',order_line=line,unit_price=1200),change.version)
        services.transition(self.manager,change,'approve',change.version)
        event=m.AuditEvent.objects.get(action='change_products')
        rows=self.context(event)['audit_differences']
        price=next((r for r in rows if r['key'].endswith('快照单价')),None)
        self.assertIsNotNone(price)
        self.assertIn(f'ID: {line.pk}',price['key'])
        self.assertEqual((price['before'],price['after']),('1000.00','1200.00'))
        self.assertNotIn('产品明细',{r['key'] for r in rows})

    def test_unique_code_takes_precedence_and_identifiers_remain_business_fields(self):
        context=self.context(self.event({'attachments':[{'code':'ATT-A','id':'1','version':'1','object_id':3}]},
                                        {'attachments':[{'code':'ATT-A','id':'2','version':'2','object_id':4}]}))
        rows=context['audit_differences']
        self.assertEqual(len(rows),2)
        self.assertTrue(all('ATT-A' in r['key'] for r in rows))
        self.assertTrue(any(r['key'].endswith('业务对象 ID') for r in rows))
        self.assertEqual(len(context['audit_technical_differences']),1)

    def test_numeric_string_ids_pair_without_a_false_identity_value_change(self):
        context=self.context(self.event({'lines':[{'id':'7','name':'原柜'}]},
                                        {'lines':[{'id':7,'name':'新柜'}]},object_type='orders'))
        self.assertEqual(len(context['audit_differences']),1)
        row=context['audit_differences'][0]
        self.assertIn('ID: 7',row['key'])
        self.assertEqual((row['before'],row['after']),('原柜','新柜'))

    def test_reorder_is_reported_separately_without_field_changes(self):
        items=[{'code':'ATT-A','name':'甲'},{'code':'ATT-B','name':'乙'}]
        rows=self.context(self.event({'attachments':items},{'attachments':list(reversed(items))}))['audit_differences']
        self.assertEqual(len(rows),1)
        self.assertIn('排列顺序',rows[0]['key'])
        self.assertNotEqual(rows[0]['before'],rows[0]['after'])

    def test_missing_duplicate_and_changed_key_sets_fall_back_without_pairing(self):
        cases=[([{'id':'1','name':'A'},{'id':'1','name':'B'}],[{'id':'1','name':'C'},{'id':'1','name':'D'}]),
               ([{'name':'A'}],[{'name':'B'}]),([{'id':'1','name':'A'}],[{'id':'2','name':'B'}])]
        for before,after in cases:
            with self.subTest(before=before):
                rows=self.context(self.event({'lines':before},{'lines':after},object_type='orders'))['audit_differences']
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['key'],'产品明细')
                self.assertIn('不推断对应关系',rows[0]['note'])

    def test_identity_labels_are_escaped_and_shared_evidence_is_identity_based(self):
        code='ATT-<script>bad()</script>'
        before=[{'code':code,'name':'共享上下文A','version':'1'},{'code':'ATT-B','name':'共享上下文B','version':'1'}]
        after=[dict(before[1]),dict(before[0],version='2')]
        event=self.event({'attachments':before},{'attachments':after})
        response=self.page(event)
        self.assertNotContains(response,'<script>bad()</script>')
        self.assertContains(response,'&lt;script&gt;bad()&lt;/script&gt;')
        self.assertContains(response,'>共享上下文A</pre>',count=1)
        self.assertContains(response,'>共享上下文B</pre>',count=1)


class AuditMeaningAndBoundaryTests(EleventhFixture):
    def test_explicit_null_blank_and_missing_are_distinct(self):
        context=self.context(self.event({'nullable':None,'note':'','archived_at':None},
                                       {'nullable':'','note':None,'archived_at':'2026-10-18T03:44:41Z','added':None}))
        rows={r['key']:r for r in context['audit_differences']}
        self.assertEqual(rows['nullable']['before'],'空值（null）')
        self.assertEqual(rows['nullable']['after'],'未填写')
        self.assertEqual(rows['备注 / 处理结论']['after'],'空值（null）')
        self.assertEqual(rows['停止共享时间']['before'],'尚未归档')
        self.assertNotIn('added',rows)
        self.assertEqual(next(r for r in context['audit_recorded_fields'] if r['key']=='added')['value'],'空值（null）')

    def test_only_known_lock_version_is_technical_not_generic_identity_changes(self):
        context=self.context(self.event({'version':'1','code':'A','object_id':3,'custom':{'version':1}},
                                       {'version':'2','code':'B','object_id':4,'custom':{'version':2}}))
        self.assertEqual(len(context['audit_technical_differences']),1)
        self.assertEqual(len(context['audit_differences']),3)
        self.assertIn('3 项业务变化',context['audit_summary'])
        self.assertTrue(any(r['key']=='custom / version' for r in context['audit_differences']))

    def test_stop_after_is_sparse_and_consumed_reason_remains_available(self):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'十一轮停止原因')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        self.assertEqual(set(event.after),{'attachment_state','attachment','reason','policy'})
        self.assertEqual(set(event.after['attachment_state']),{'version','archived_at','archive_reason'})
        self.assertIn('handoff',event.before)
        self.assertEqual(event.before['attachment_state']['sha256'],item.sha256)
        context=self.context(event)
        self.assertEqual(len(context['audit_differences']),2)
        self.assertEqual(len(context['audit_technical_differences']),1)
        self.assertEqual(sum(r['stage']=='变更后' for r in context['audit_recorded_fields']),2)
        self.assertTrue(any(r['value']==item.code for r in context['audit_shared_rows']))
        self.assertEqual(next(r for r in context['audit_differences'] if r['key'].endswith('归档时间'))['before'],'尚未归档')
        self.client.force_login(self.manager)
        shown=self.client.get(f'/modules/handoffs/{self.handoff.pk}/').context['attachments'][0]
        self.assertEqual(shown.stop_reason,'十一轮停止原因')

    def test_delete_after_has_no_unused_time_or_hash_aliases(self):
        item=self.upload(self.sales)
        evidence.delete_attachment(self.sales,item,item.version)
        event=m.AuditEvent.objects.get(action='delete_attachment')
        self.assertEqual(set(event.after),{'attachment_state','attachment'})
        self.assertEqual(set(event.after['attachment_state']),{'version','deleted_at'})
        context=self.context(event)
        self.assertEqual(len(context['audit_differences']),1)
        self.assertEqual(len(context['audit_technical_differences']),1)
        self.assertEqual(context['audit_differences'][0]['before'],'尚未删除')

    def test_list_and_csv_are_compact_local_minutes_while_audit_info_is_detailed(self):
        moment=datetime(2026,10,18,3,44,41,tzinfo=datetime_timezone.utc)
        m.Handoff.objects.filter(pk=self.handoff.pk).update(created_at=moment)
        event=self.event({}, {'note':'时间边界'})
        m.AuditEvent.objects.filter(pk=event.pk).update(created_at=moment)
        self.client.force_login(self.manager)
        with timezone.override('Asia/Shanghai'):
            response=self.client.get('/modules/handoffs/')
            self.assertContains(response,'2026-10-18 11:44')
            self.assertNotContains(response,'11:44:41+08:00')
            exported=self.client.get('/modules/handoffs/export/')
            self.assertEqual(exported.status_code,200)
            rows=list(csv.reader(io.StringIO(exported.content.decode('utf-8-sig'))))
            self.assertEqual(rows[1][-1],'2026-10-18 11:44')
            datetime.strptime(rows[1][-1],'%Y-%m-%d %H:%M')
            self.assertContains(self.page(event),'2026-10-18 11:44:41+08:00')

    def test_known_module_and_action_labels_are_chinese_with_raw_codes_preserved(self):
        event=self.event({}, {'note':'中文标签'},object_type='orders',action='revise_payment_plan')
        response=self.page(event)
        self.assertContains(response,'合同与订单')
        self.assertContains(response,'修订分期计划（金额变化）')
        self.assertNotContains(response,'>orders<')
        self.assertNotContains(response,'>revise_payment_plan<')
        self.assertIn('演示数据',action_name('demo_second_snapshot'))
        self.assertEqual(action_name('unknown_action'),'unknown_action')
        event.refresh_from_db();self.assertEqual(event.object_type,'orders');self.assertEqual(event.action,'revise_payment_plan')


class AuditFactProjectionTests(EleventhFixture):
    def test_recorded_subsets_do_not_repeat_in_full_tables(self):
        response=self.page(self.event({'note':'旧值','before_only':'前侧唯一'}, {'note':'新值','after_only':'后侧唯一'}))
        self.assertContains(response,'>前侧唯一</pre>',count=1)
        self.assertContains(response,'>后侧唯一</pre>',count=1)
        self.assertContains(response,'>旧值</pre>',count=1)
        self.assertContains(response,'>新值</pre>',count=1)
        self.assertContains(response,'原始记录 · JSON')

    def test_common_unchanged_context_is_presented_once_and_full_context_retained(self):
        response=self.page(self.event({'note':'旧值','custom':'两侧相同文本'}, {'note':'新值','custom':'两侧相同文本'}))
        self.assertContains(response,'>两侧相同文本</pre>',count=1)
        self.assertEqual(len(response.context['audit_shared_rows']),1)
        self.assertEqual(response.context['audit_payloads'][0]['rows'][1]['raw'],'两侧相同文本')

    def test_aggregate_fallback_never_hides_unmatched_list_details(self):
        context=self.context(self.event({'lines':[{'name':'原产品','unit_price':'100'}]},
                                       {'lines':[{'name':'新产品','unit_price':'200'}]},object_type='orders'))
        self.assertEqual(len(context['audit_differences']),1)
        for payload in context['audit_payloads']:
            self.assertTrue(payload['show'])
            self.assertEqual(len(payload['render_rows']),2)
