from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from erp import models as m,services
from erp.services import collaboration,receivables,pricing
from .base import ERPTestCase


class RecoveryTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.factory=m.Organization.objects.create(name='接单工厂',kind='factory')
        self.planner=User.objects.create_user('receiver')
        m.Membership.objects.create(user=self.planner,organization=self.factory,role='planner',scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)

    def sent(self):
        order=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        return order,collaboration.send_handoff(self.sales,order,self.factory,order.version)

    def test_source_can_withdraw_sent_and_cancel_without_factory_reply(self):
        order,handoff=self.sent()
        self.assertIn('withdraw',dict(services.available_actions(handoff)))
        services.transition(self.sales,handoff,'withdraw',handoff.version,'客户暂缓')
        handoff.refresh_from_db();order.refresh_from_db()
        self.assertEqual(handoff.status,'withdrawn')
        services.transition(self.sales,order,'cancel',order.version,'撤单')
        self.assertFalse(m.Order.objects.filter(source_handoff=handoff).exists())
        self.assertTrue(m.AuditEvent.objects.filter(action='withdraw_handoff').exists())

    def test_disabled_route_still_allows_source_withdraw_and_later_resend(self):
        order,handoff=self.sent();self.route.enabled=False;self.route.save()
        services.transition(self.sales,handoff,'withdraw',handoff.version,'合作授权停用')
        order.refresh_from_db();handoff.refresh_from_db()
        self.route.enabled=True;self.route.save()
        sent=collaboration.send_handoff(self.sales,order,self.factory,order.version,handoff_version=handoff.version)
        self.assertEqual(sent.status,'sent')
        with self.assertRaises(ValidationError):services.transition(self.sales,sent,'withdraw',1,'旧版本')

    def test_target_can_reject_after_route_disabled_but_cannot_accept(self):
        order,handoff=self.sent();self.route.enabled=False;self.route.save()
        with self.assertRaises(PermissionDenied):collaboration.accept_handoff(self.planner,handoff,handoff.version)
        services.transition(self.planner,handoff,'reject',handoff.version,'协作授权停用')
        handoff.refresh_from_db();self.assertEqual(handoff.status,'rejected')
        self.assertFalse(m.Order.objects.filter(source_handoff=handoff).exists())

    def test_factory_cannot_withdraw_source_order_or_accept_after_withdrawal(self):
        order,handoff=self.sent()
        with self.assertRaises(PermissionDenied):services.transition(self.planner,handoff,'withdraw',handoff.version,'越权')
        services.transition(self.sales,handoff,'withdraw',handoff.version,'客户暂缓')
        with self.assertRaises((ValidationError,PermissionDenied)):collaboration.accept_handoff(self.planner,handoff,1)
        from erp.permissions import scoped
        self.assertFalse(scoped(self.planner,m.Handoff).filter(pk=handoff.pk).exists())

    def test_generic_handoff_http_actions_use_source_and_factory_permissions(self):
        order,handoff=self.sent()
        self.client.force_login(self.sales)
        self.assertContains(self.client.get(f'/modules/handoffs/{handoff.pk}/'),'撤回交接')
        self.client.post(f'/modules/handoffs/{handoff.pk}/action/',{'action':'withdraw','version':handoff.version,'note':'暂缓'})
        handoff.refresh_from_db();self.assertEqual(handoff.status,'withdrawn')

    def test_progress_uses_same_source_before_handoff_lock_order(self):
        from unittest.mock import patch
        order,handoff=self.sent()
        local=collaboration.accept_handoff(self.planner,handoff,handoff.version)
        order_lock=m.Order.objects.select_for_update;handoff_lock=m.Handoff.objects.select_for_update
        acquired=[]
        def lock_order(*a,**kw):acquired.append('order');return order_lock(*a,**kw)
        def lock_handoff(*a,**kw):acquired.append('handoff');return handoff_lock(*a,**kw)
        with patch.object(m.Order.objects,'select_for_update',side_effect=lock_order),patch.object(m.Handoff.objects,'select_for_update',side_effect=lock_handoff):
            collaboration.return_progress(self.planner,local,'producing')
        self.assertLess(acquired.index('order'),acquired.index('handoff'))

    def test_real_requester_can_withdraw_pending_change(self):
        order=self.order(status='confirmed')
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=1000,
                         proposed_due_date=order.due_date,note='客户改单',owner=self.sales)
        with self.assertRaises((PermissionDenied,ValidationError)):services.transition(self.manager,change,'withdraw',change.version,'非申请人')
        services.transition(self.sales,change,'withdraw',change.version,'客户不再变更')
        change.refresh_from_db();self.assertEqual(change.status,'withdrawn')
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)


class FinancialRevisionTests(ERPTestCase):
    def priced(self,known=True):
        order=self.order(status='confirmed')
        if hasattr(order,'discount_confirmed'):order.discount_confirmed=known;order.save()
        line=m.OrderLine.objects.create(order=order,name='衣柜',quantity=1,unit_price=1000,line_total=1000)
        return order,line

    def change(self,order,**values):
        order.refresh_from_db()
        return self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=order.total,
                         proposed_due_date=order.due_date,note='客户确认价格调整',**values)

    def test_amount_change_resizes_unpaid_periods_without_moving_deposit(self):
        order,line=self.priced()
        receivables.create_plan(self.manager,order,order.version,date(2026,10,20),[30,40,30])
        services.record_payment(self.finance,order,300,'DEPOSIT',date.today(),'银行','')
        change=self.change(order,revise_payment_plan=True)
        services.add_change_line(self.admin,change,dict(operation='update',order_line=line,quantity=2),change.version)
        change.refresh_from_db();services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertEqual(order.total,2000)
        rows=receivables.plan_rows(order)
        self.assertEqual([row['plan'].amount for row in rows],[Decimal(300),Decimal('971.43'),Decimal('728.57')])
        self.assertEqual([row['paid'] for row in rows],[Decimal(300),0,0])
        self.assertTrue(m.AuditEvent.objects.filter(action='revise_payment_plan').exists())

    def test_discount_correction_preserves_fee_and_source_quote(self):
        quote=self.make(m.Quote,customer=self.customer,title='折扣修正',discount=Decimal('.95'))
        for values in [dict(name='柜体',quantity=1,unit_price=1000),dict(name='运费',quantity=1,unit_price=200,discountable=False)]:
            services.add_line(self.admin,quote,values,quote.version);quote.refresh_from_db()
        for action in ('submit','approve','accept'):
            services.transition(self.admin,quote,action,quote.version);quote.refresh_from_db()
        order=m.Order.objects.get(quote=quote);order.status='confirmed';order.due_date=date(2026,12,1);order.save()
        change=self.change(order,proposed_discount=Decimal('.9'))
        change.proposed_total=1100;change.save()
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();quote.refresh_from_db()
        self.assertEqual(order.total,1100);self.assertEqual(order.discount,Decimal('.9'))
        self.assertTrue(order.discount_confirmed)
        self.assertEqual(order.lines.get(name='运费').line_total,200)
        self.assertEqual(quote.discount,Decimal('.95'));self.assertEqual(quote.total,1150)

    def test_unknown_basis_blocks_pricing_until_explicit_correction(self):
        order,line=self.priced(known=False)
        change=self.change(order)
        with self.assertRaises(ValidationError):services.add_change_line(self.admin,change,dict(operation='add',name='台面',quantity=1,unit_price=300),change.version)
        change=self.change(order,proposed_discount=Decimal('.8'))
        change.proposed_total=800;change.save()
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertEqual(order.total,800);self.assertTrue(order.discount_confirmed)

    def test_penny_reduction_never_creates_negative_period_or_changes_receipts(self):
        order,line=self.priced();receivables.create_plan(self.manager,order,order.version,date.today(),[30,40,30])
        change=self.change(order,revise_payment_plan=True)
        services.add_change_line(self.admin,change,dict(operation='update',order_line=line,unit_price=Decimal('.01')),change.version)
        change.refresh_from_db();services.transition(self.manager,change,'approve',change.version)
        amounts=list(order.payment_plans.values_list('amount',flat=True))
        self.assertEqual(sum(amounts),Decimal('.01'));self.assertTrue(all(amount>=0 for amount in amounts))

    def test_no_snapshot_requires_explicit_full_restore(self):
        order=self.order(status='confirmed')
        change=self.change(order,proposed_discount=Decimal('.8'))
        with self.assertRaises(ValidationError):services.add_change_line(self.admin,change,dict(operation='add',name='完整柜体',quantity=1,unit_price=1250),change.version)
        change.restore_product_snapshot=True;change.save()
        services.add_change_line(self.admin,change,dict(operation='add',name='完整柜体',quantity=1,unit_price=1250),change.version)
        change.refresh_from_db();self.assertEqual(change.proposed_total,1000)
        services.transition(self.manager,change,'approve',change.version)
        self.assertEqual(order.lines.get().line_total,1000)

    def test_http_discount_creation_computes_total_and_edit_refreshes_pending_lines(self):
        order,line=self.priced()
        self.client.force_login(self.admin)
        values=dict(organization=self.org.pk,owner=self.admin.pk,order=order.pk,proposed_discount='.9',
                    proposed_due_date='2026-12-01',note='合同价格口径核对',version=1)
        response=self.client.post('/modules/changes/new/',values)
        self.assertEqual(response.status_code,302, response.context['form'].errors if response.context else '')
        change=m.ChangeRequest.objects.latest('pk');self.assertEqual(change.proposed_total,900)
        services.add_change_line(self.admin,change,dict(operation='update',order_line=line,quantity=2),change.version)
        change.refresh_from_db();values.update(proposed_discount='.8',version=change.version)
        response=self.client.post(f'/modules/changes/{change.pk}/edit/',values)
        self.assertEqual(response.status_code,302)
        change.refresh_from_db();self.assertEqual(change.proposed_total,1600)
        self.assertEqual(change.lines.get().net_amount,1600)

    def test_resize_requires_explicit_intent_and_cannot_reduce_below_paid(self):
        order,line=self.priced();receivables.create_plan(self.manager,order,order.version,date.today(),[30,40,30])
        services.record_payment(self.finance,order,700,'PAID',date.today(),'银行','')
        change=self.change(order)
        services.add_change_line(self.admin,change,dict(operation='update',order_line=line,unit_price=800),change.version)
        change.refresh_from_db()
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
        change.revise_payment_plan=True;change.proposed_total=600;change.save()
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertEqual(order.total,1000)
        self.assertEqual(sum(order.payment_plans.values_list('amount',flat=True)),1000)

    def test_settled_contract_addition_keeps_all_receipts_and_assigns_tail(self):
        order,line=self.priced();receivables.create_plan(self.manager,order,order.version,date.today(),[30,40,30])
        services.record_payment(self.finance,order,1000,'SETTLED',date.today(),'银行','')
        old=[row['paid'] for row in receivables.plan_rows(order)]
        change=self.change(order,revise_payment_plan=True)
        services.add_change_line(self.admin,change,dict(operation='add',name='新增安装',quantity=1,unit_price=100,discountable=False),change.version)
        change.refresh_from_db();services.transition(self.manager,change,'approve',change.version)
        rows=receivables.plan_rows(order)
        self.assertEqual([r['paid'] for r in rows],old)
        self.assertEqual([r['balance'] for r in rows],[0,0,100])


    def test_unknown_no_product_header_amount_cannot_bypass_basis(self):
        order=self.order(status='confirmed');order.discount_confirmed=False;order.save()
        change=self.change(order);change.proposed_total=1500;change.save()
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();self.assertEqual(order.total,1000)


class ExpandedEvidenceTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.factory=m.Organization.objects.create(name='接单工厂',kind='factory')
        self.planner=User.objects.create_user('receiver')
        m.Membership.objects.create(user=self.planner,organization=self.factory,role='planner',scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)

    sent=RecoveryTests.sent
    def test_handoff_evidence_requires_explicit_safe_sharing_and_revokes_on_withdraw(self):
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        from erp.services import evidence
        order,handoff=self.sent()
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            def file():return SimpleUploadedFile('规格.pdf',b'%PDF-1.7 fixture')
            with self.assertRaises(ValidationError):evidence.upload_attachment(self.sales,handoff,file(),'drawing')
            with self.assertRaises(ValidationError):evidence.upload_attachment(self.sales,handoff,file(),'contract',share_confirmed=True,parent_version=handoff.version,target_id=handoff.target_organization_id)
            item=evidence.upload_attachment(self.sales,handoff,file(),'drawing',share_confirmed=True,parent_version=handoff.version,target_id=handoff.target_organization_id)
            self.assertEqual(evidence.authorized_attachment(self.planner,item.pk).pk,item.pk)
            services.transition(self.sales,handoff,'withdraw',handoff.version,'客户调整')
            with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.planner,item.pk)
            self.assertEqual(evidence.authorized_attachment(self.sales,item.pk).pk,item.pk)

    def test_new_evidence_parents_recheck_scoped_business_permission(self):
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        from erp.services import evidence
        order=self.order(status='confirmed')
        purchase=self.make(m.Purchase,supplier='供应商',warehouse=self.warehouse)
        bom=self.make(m.Breakdown,order=order)
        parents=[purchase,self.make(m.Production,breakdown=bom),
                 self.make(m.PurchaseReceipt,purchase=purchase,reference='ARRIVAL'),
                 self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=1000,proposed_due_date=order.due_date)]
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            for parent in parents:
                item=evidence.upload_attachment(self.admin,parent,SimpleUploadedFile('凭证.pdf',b'%PDF-1.7 fixture'),'contract')
                with self.assertRaises(PermissionDenied):evidence.authorized_attachment(User.objects.create_user(f'outsider-{item.pk}'),item.pk)
                self.assertEqual(evidence.authorized_attachment(self.admin,item.pk).pk,item.pk)


    def test_stale_handoff_upload_cannot_share_old_file_with_new_factory(self):
        from tempfile import TemporaryDirectory
        from django.test import override_settings
        from django.core.files.uploadedfile import SimpleUploadedFile
        from erp.services import evidence
        order,handoff=self.sent()
        old_version,old_target=handoff.version,handoff.target_organization_id
        services.transition(self.sales,handoff,'withdraw',handoff.version,'改派')
        order.refresh_from_db();handoff.refresh_from_db()
        other=m.Organization.objects.create(name='另一工厂',kind='factory')
        collaboration.send_handoff(self.admin,order,other,order.version,handoff_version=handoff.version)
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            with self.assertRaises(ValidationError):
                evidence.upload_attachment(self.sales,handoff,SimpleUploadedFile('旧厂图纸.pdf',b'%PDF-1.7 fixture'),'drawing',
                    share_confirmed=True,parent_version=old_version,target_id=old_target)
        self.assertFalse(m.Attachment.objects.filter(object_type='handoffs',object_id=handoff.pk).exists())


class SeedValidationTests(ERPTestCase):
    def test_missing_factory_is_readable_and_rolls_back(self):
        from django.core.management import call_command,CommandError
        from django.test import override_settings
        self.make(m.AuditEvent,action='demo_seed',object_type='system',object_id=0)
        with override_settings(DEBUG=True),self.assertRaisesMessage(CommandError,'工厂'):
            call_command('seed_upgrade_demo')
        self.assertFalse(m.CollaborationRoute.objects.exists())
