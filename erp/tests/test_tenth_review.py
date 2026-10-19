"""Meaningful governance outcomes, local timestamps and complete evidence without repeats."""
from datetime import datetime, timezone as datetime_timezone
from unittest.mock import patch
from django.utils import timezone
from erp import models as m
from erp.audit_display import audit_context
from erp.services import evidence
from erp.views.modules import display_value
from .test_eighth_review import EighthReviewFixture


class TenthReviewFixture(EighthReviewFixture):
    def event(self,before,after,object_type='handoffs'):
        return self.make(m.AuditEvent,object_type=object_type,object_id=self.handoff.pk,
                         code=self.handoff.code,action='test',before=before,after=after)

    def context(self,event):
        return audit_context(self.manager,event,self.org.pk)

    def page(self,event):
        self.client.force_login(self.manager)
        return self.client.get(f'/modules/audit/{event.pk}/')


class GovernanceOutcomeTests(TenthReviewFixture):
    def test_stop_records_explicit_nullable_state_and_keeps_source_reason_lookup(self):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'十次误传')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        self.assertIn('attachment_state',event.before)
        self.assertIsNone(event.before['attachment_state']['archived_at'])
        self.assertIsNone(event.before['attachment_state']['deleted_at'])
        self.assertIsNotNone(event.after['attachment_state']['archived_at'])
        self.assertEqual(event.after['attachment'],item.code)
        self.assertEqual(event.after['reason'],'十次误传')
        self.assertEqual(event.before['attachment_state']['sha256'],item.sha256)
        rows={r['key']:r for r in self.context(event)['audit_differences']}
        self.assertEqual(set(rows),{'附件状态快照 / 归档时间','附件状态快照 / 归档原因类别'})
        self.assertEqual(len(self.context(event)['audit_technical_differences']),1)
        self.assertEqual(rows['附件状态快照 / 归档时间']['before'],'尚未归档')
        self.assertEqual(rows['附件状态快照 / 归档原因类别']['after'],'人工停止共享')
        self.client.force_login(self.manager)
        response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        shown=next(f for f in response.context['attachments'] if f.pk==item.pk)
        self.assertEqual(shown.stop_reason,'十次误传')
        self.assertEqual(shown.stop_actor,self.manager.username)
        audit_page=self.page(event)
        self.assertEqual(audit_page.status_code,200)
        self.assertContains(audit_page,'2 项业务变化 · 1 项校验信息更新 · 3 个结果字段')
        self.assertContains(audit_page,'人工停止共享')

    def test_delete_compares_version_and_explicit_delete_time_and_keeps_flat_keys(self):
        item=self.upload(self.sales)
        evidence.delete_attachment(self.sales,item,item.version)
        event=m.AuditEvent.objects.get(action='delete_attachment')
        self.assertIn('attachment_state',event.before)
        self.assertIsNone(event.before['attachment_state']['deleted_at'])
        self.assertEqual(event.after['attachment'],item.code)
        self.assertEqual(event.before['attachment_state']['sha256'],item.sha256)
        rows={r['key']:r for r in self.context(event)['audit_differences']}
        self.assertEqual(set(rows),{'附件状态快照 / 删除时间'})
        self.assertEqual(rows['附件状态快照 / 删除时间']['before'],'尚未删除')
        technical=self.context(event)['audit_technical_differences']
        self.assertEqual(len(technical),1)
        self.assertEqual(technical[0]['before'],'1');self.assertEqual(technical[0]['after'],'2')
        self.assertContains(self.page(event),'1 项业务变化 · 1 项校验信息更新 · 1 个结果字段')

    def test_audit_write_failure_rolls_back_attachment_and_epoch(self):
        item=self.upload(self.receiver)
        epoch=evidence.evidence_epoch(self.handoff)
        with patch('erp.services.evidence.audit',side_effect=RuntimeError('audit failure')):
            with self.assertRaises(RuntimeError):
                evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'须全部回滚')
        item.refresh_from_db()
        self.assertIsNone(item.archived_at);self.assertEqual(item.version,1)
        self.assertEqual(evidence.evidence_epoch(self.handoff),epoch)
        self.assertFalse(m.AuditEvent.objects.filter(action='stop_sharing_attachment').exists())


class AuditOutcomePresentationTests(TenthReviewFixture):
    def test_equal_count_list_content_order_or_type_changes_are_distinguishable(self):
        for before,after in (([{'name':'A'}],[{'name':'B'}]),([1,2],[2,1]),([False],[0])):
            with self.subTest(before=before,after=after):
                rows=self.context(self.event({'lines':before},{'lines':after}))['audit_differences']
                self.assertEqual(len(rows),1)
                self.assertNotEqual(rows[0]['before'],rows[0]['after'])
                self.assertIn('条数不变',rows[0]['after'])
                self.assertIn('内容或顺序有变化',rows[0]['after'])

    def test_known_aware_timestamp_is_local_with_offset_and_raw_precision_preserved(self):
        raw='2026-10-18 03:44:41.264926+00:00'
        event=self.event({}, {'archived_at':raw,'deleted_at':'2026-10-18T03:44:41Z','note':raw})
        with timezone.override('Asia/Shanghai'):
            context=self.context(event)
        values={r['key']:r['value'] for r in context['audit_payloads'][1]['rows']}
        self.assertEqual(values['停止共享时间'],'2026-10-18 11:44:41+08:00')
        self.assertEqual(values['删除时间'],'2026-10-18 11:44:41+08:00')
        self.assertEqual(values['备注 / 处理结论'],raw)
        self.assertIn(raw,context['audit_raw'])
        event.refresh_from_db();self.assertEqual(event.after['archived_at'],raw)

    def test_naive_invalid_or_out_of_range_times_are_not_given_guessed_zones(self):
        event=self.event({}, {'archived_at':'2026-10-18 03:44:41'})
        value=self.context(event)['audit_payloads'][1]['rows'][0]['value']
        self.assertIn('时区未记录',value)
        for invalid in ('错误时间','2026-99-99 00:00:00+00:00','9999-12-31T23:59:59Z'):
            with self.subTest(invalid=invalid),timezone.override('Asia/Shanghai'):
                value=self.context(self.event({}, {'archived_at':invalid}))['audit_payloads'][1]['rows'][0]['value']
                self.assertEqual(value,invalid)

    def test_info_datetime_and_payload_share_timezone_without_shifting_dates(self):
        self.handoff.created_at=datetime(2026,10,18,3,44,41,tzinfo=datetime_timezone.utc)
        with timezone.override('Asia/Shanghai'):
            self.assertEqual(display_value(self.handoff,'created_at',detailed=True),'2026-10-18 11:44:41+08:00')
            self.assertEqual(display_value(self.source,'due_date'),self.source.due_date.strftime('%Y-%m-%d'))

    def test_target_alias_enriches_only_authorized_organization_names(self):
        secret=m.Organization.objects.create(name='十次无权工厂',kind='factory')
        context=self.context(self.event({'target':self.factory.pk},{'target':secret.pk}))
        row=context['audit_differences'][0]
        self.assertEqual(row['before'],f'{self.factory.name}（ID: {self.factory.pk}）')
        self.assertEqual(row['after'],f'ID: {secret.pk}（当前名称不可用）')
        self.assertNotIn(secret.name,str(context['audit_payloads']))
        # A non-ID target name from the older order send event stays a name.
        context=self.context(self.event({}, {'target':'已记录工厂名称'},object_type='orders'))
        self.assertEqual(context['audit_payloads'][1]['rows'][0]['value'],'已记录工厂名称')

    def test_historical_partial_outcome_count_does_not_lead_with_zero_changes(self):
        event=self.event({'handoff':{'code':'OLD','status':'rejected'}},
                         {'attachment':'A','reason':'误传','archived_at':'2026-10-18T03:44:41Z'})
        response=self.page(event)
        self.assertContains(response,'<h2>本次操作记录</h2>',html=True)
        self.assertContains(response,'3 个结果字段')
        self.assertNotContains(response,'0 项可比变化')
        self.assertEqual(response.context['audit_differences'],[])

    def test_fully_repeated_side_tables_are_merged_but_raw_evidence_is_retained(self):
        event=self.event({'note':'前侧唯一文本'},{'reason':'后侧唯一文本'})
        response=self.page(event)
        self.assertContains(response,'>前侧唯一文本</pre>',count=1)
        self.assertContains(response,'>后侧唯一文本</pre>',count=1)
        self.assertContains(response,'已含完整变更前快照')
        self.assertContains(response,'已含完整变更后快照')
        self.assertNotContains(response,'<summary>完整变更前快照</summary>',html=True)
        self.assertContains(response,'原始记录 · JSON')

    def test_writes_have_no_empty_before_panel_and_nonwrites_have_no_write_rows(self):
        response=self.page(self.event({}, {'reason':'直接写入'}))
        self.assertNotContains(response,'完整变更前快照')
        self.assertNotContains(response,'未记录此阶段快照')
        self.assertNotContains(response,'<summary>完整变更后快照</summary>',html=True)
        self.assertContains(response,'原始记录 · JSON')
        context=self.context(self.event({'note':'A'},{'note':'B'}))
        self.assertEqual(context['audit_write_rows'],[])

    def test_single_sided_list_has_one_summary_and_full_translated_details(self):
        context=self.context(self.event({'note':'以前的部分记录'},
            {'lines':[{'name':'柜A','unit_price':'100'},{'name':'柜B','unit_price':'200'}]},object_type='orders'))
        after=[r for r in context['audit_recorded_fields'] if r['stage']=='变更后']
        self.assertEqual(len(after),1)
        self.assertEqual(after[0]['key'],'产品明细')
        self.assertEqual(after[0]['value'],'2 条（内容见完整快照）')
        self.assertTrue(context['audit_payloads'][1]['show'])
        self.assertIn('产品明细 / 第1项 / 快照单价',{r['key'] for r in context['audit_payloads'][1]['rows']})
