"""Audit truthfulness with real service payloads and incomplete historical evidence."""
from erp import models as m, services
from erp.audit_display import audit_context
from erp.services import evidence, pricing
from erp.services.common import snapshot
from django.contrib.auth.models import User
from .test_eighth_review import EighthReviewFixture


class AuditPayloadSemanticsTests(EighthReviewFixture):
    def context(self,event):
        return audit_context(self.manager,event,self.org.pk)

    def event(self,before,after):
        return self.make(m.AuditEvent,object_type='handoffs',object_id=self.handoff.pk,
                         code=self.handoff.code,action='test',before=before,after=after)

    def page(self,event):
        self.client.force_login(self.manager)
        return self.client.get(f'/modules/audit/{event.pk}/')

    def test_real_stop_sharing_does_not_claim_handoff_or_attachment_were_cleared(self):
        item=self.upload(self.receiver)
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'误传')
        event=m.AuditEvent.objects.get(action='stop_sharing_attachment')
        context=self.context(event)
        self.assertEqual({r['key'] for r in context['audit_differences']},
                         {'附件状态快照 / 归档时间','附件状态快照 / 归档原因类别'})
        self.assertEqual(len(context['audit_technical_differences']),1)
        recorded={r['key']:r['value'] for r in context['audit_recorded_fields'] if r['stage']=='变更后'}
        self.assertEqual(set(recorded),{'附件编号','停止原因','处理规则'})
        self.assertEqual(recorded['附件编号'],item.code)
        self.assertEqual(recorded['停止原因'],'误传')
        self.assertEqual(self.handoff.code,event.before['handoff']['code'])
        response=self.page(event)
        self.assertContains(response,'缺少键表示未记录')
        self.assertNotContains(response,'未记录此字段')
        event.refresh_from_db();self.assertEqual(event.after['reason'],'误传')

    def test_real_delete_attachment_shows_deleted_time_without_inferred_deletions(self):
        item=self.upload(self.sales)
        evidence.delete_attachment(self.sales,item,item.version)
        event=m.AuditEvent.objects.get(action='delete_attachment')
        context=self.context(event)
        self.assertEqual({r['key'] for r in context['audit_differences']},
                         {'附件状态快照 / 删除时间'})
        self.assertEqual(len(context['audit_technical_differences']),1)
        self.assertNotIn('删除时间',{r['key'] for r in context['audit_recorded_fields']})
        item.refresh_from_db();self.assertIsNotNone(item.deleted_at)

    def test_real_remove_line_compares_change_and_keeps_removed_line_as_evidence(self):
        self.source.refresh_from_db()
        change=self.make(m.ChangeRequest,order=self.source,order_version=self.source.version,
                         proposed_total=1000,proposed_due_date=self.source.due_date)
        change=pricing.add_change_line(self.admin,change,dict(operation='add',name='安装',quantity=1,unit_price=200),change.version)
        pricing.remove_change_line(self.admin,change,change.lines.get().pk,change.version)
        context=self.context(m.AuditEvent.objects.get(action='remove_change_line'))
        self.assertTrue(context['audit_differences'])
        self.assertFalse(any(r['key'].startswith('申请明细快照') for r in context['audit_differences']))
        totals=[r for r in context['audit_differences'] if r['key']=='变更申请快照 / 变更后金额']
        self.assertEqual(len(totals),1)
        self.assertEqual(totals[0]['before'],'1200.00');self.assertEqual(totals[0]['after'],'1000.00')
        self.assertTrue(any(r['key'].startswith('申请明细快照') for r in context['audit_recorded_fields']))

    def test_real_product_clear_is_one_list_change_with_original_snapshot_retained(self):
        order=self.order(status='confirmed')
        line=m.OrderLine.objects.create(order=order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,
                         proposed_total=1000,proposed_due_date=order.due_date)
        change=pricing.add_change_line(self.admin,change,dict(operation='remove',order_line=line),change.version)
        services.transition(self.manager,change,'approve',change.version)
        event=m.AuditEvent.objects.get(action='change_products')
        rows=self.context(event)['audit_differences']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['key'],'产品明细')
        self.assertEqual(rows[0]['before'],'1 条（内容见完整快照）')
        self.assertEqual(rows[0]['after'],'0 条（已清空）')
        self.assertEqual(event.before['lines'][0]['name'],'柜体')
        self.assertEqual(event.after['lines'],[])

    def test_root_and_nested_missing_fields_are_single_sided_evidence(self):
        context=self.context(self.event({'note':'旧','handoff':{'code':'旧号','note':'原备注'}},
                                        {'handoff':{'note':'新备注'},'new_value':None}))
        rows=context['audit_differences']
        self.assertEqual([r['key'] for r in rows],['交接快照 / 备注 / 处理结论'])
        single={(r['stage'],r['key']):r['value'] for r in context['audit_recorded_fields']}
        self.assertEqual(single[('变更前','备注 / 处理结论')],'旧')
        self.assertEqual(single[('变更前','交接快照 / 单据编号')],'旧号')
        self.assertEqual(single[('变更后','new_value')],'空值（null）')

    def test_shape_change_is_recorded_without_inferred_leaf_removals(self):
        context=self.context(self.event({'attachment':{'code':'A','organization':str(self.org.pk)}}, {'attachment':'A'}))
        self.assertEqual(context['audit_differences'],[])
        self.assertEqual(len(context['audit_recorded_fields']),3)

    def test_list_changes_are_atomic_for_same_length_and_reordered_items(self):
        for after in ([{'name':'B'},{'name':'A'}],[{'name':'A'},{'name':'C'}],[]):
            with self.subTest(after=after):
                rows=self.context(self.event({'lines':[{'name':'A'},{'name':'B'}]},{'lines':after}))['audit_differences']
                self.assertEqual(len(rows),1)
                self.assertEqual(rows[0]['key'],'产品明细')

    def test_empty_before_upload_is_write_content_without_empty_before_columns(self):
        item=self.upload(self.sales)
        event=m.AuditEvent.objects.get(action='upload_attachment')
        response=self.page(event)
        self.assertContains(response,'<h2>本次写入</h2>',html=True)
        self.assertNotContains(response,'<small>变更前</small>',html=True)
        self.assertEqual(response.context['audit_differences'],[])
        self.assertContains(response,item.code)

    def test_root_list_and_nested_boolean_changes_have_one_named_summary(self):
        event=self.event([{'flag':False}],[{'flag':0}])
        rows=self.context(event)['audit_differences']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['key'],'内容')
        self.assertEqual(rows[0]['before'],'1 条（内容见完整快照）')
        self.assertEqual(rows[0]['after'],'1 条（条数不变，内容或顺序有变化；见完整快照）')
        self.assertNotEqual(rows[0]['before'],rows[0]['after'])

    def test_null_boolean_and_empty_containers_keep_explicit_semantics(self):
        context=self.context(self.event({'nullable':None,'flag':False,'zero':0,'lines':[], 'custom':{}},
                                       {'nullable':'','flag':0,'zero':False,'lines':[{'name':'柜体'}], 'custom':{'note':'已记'}}))
        rows={r['key']:r for r in context['audit_differences']}
        self.assertEqual(rows['nullable']['before'],'空值（null）');self.assertEqual(rows['nullable']['after'],'未填写')
        self.assertEqual(rows['flag']['before'],'否');self.assertEqual(rows['flag']['after'],'0')
        self.assertEqual(rows['zero']['before'],'0');self.assertEqual(rows['zero']['after'],'否')
        self.assertEqual(rows['产品明细']['before'],'0 条（无条目）')
        self.assertEqual(len(context['audit_differences']),4)


class AuditOrganizationNamesTests(EighthReviewFixture):
    def context(self,user,payload):
        event=self.make(m.AuditEvent,object_type='handoffs',object_id=self.handoff.pk,code=self.handoff.code,
                        action='test',after=payload)
        return audit_context(user,event,self.org.pk)

    def rendered_values(self,context):
        return ' '.join(r['value'] for p in context['audit_payloads'] for r in p['rows'])

    def test_source_manager_resolves_actual_visible_handoff_counterpart(self):
        values=self.rendered_values(self.context(self.manager,{'handoff':snapshot(self.handoff)}))
        self.assertIn(f'{self.factory.name}（ID: {self.factory.pk}）',values)

    def test_tree_accessible_sibling_is_resolved_despite_active_organization(self):
        group=m.Organization.objects.create(name='九次集团',kind='company')
        sibling=m.Organization.objects.create(name='九次兄弟门店',parent=group)
        self.org.parent=group;self.org.save()
        tree=User.objects.create_user('ninth-tree')
        m.Membership.objects.create(user=tree,organization=group,role='manager',scope='tree')
        values=self.rendered_values(self.context(tree,{'handoff':{'organization':str(sibling.pk)}}))
        self.assertIn(f'{sibling.name}（ID: {sibling.pk}）',values)

    def test_forged_counterpart_and_unrelated_organization_are_not_authorized_by_payload(self):
        secret=m.Organization.objects.create(name='九次无权组织')
        values=self.rendered_values(self.context(self.manager,{'handoff':{'target_organization':str(secret.pk)}}))
        self.assertNotIn(secret.name,values)
        self.assertIn('当前名称不可用',values)
        self.status('withdrawn')
        values=self.rendered_values(self.context(self.receiver,{'handoff':snapshot(self.handoff)}))
        self.assertNotIn(self.org.name,values)

    def test_future_closed_status_has_matching_page_guidance_and_upload_denial(self):
        self.status('future_closed')
        self.client.force_login(self.manager)
        response=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        self.assertContains(response,'如需新增共享，请由源门店重新发送交接')
        self.assertNotContains(response,'type="file"')
        self.assertFalse(evidence.evidence_permissions(self.manager,self.handoff).upload)
