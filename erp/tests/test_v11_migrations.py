"""Upgrade real historical rows, not just an empty database schema."""
from decimal import Decimal
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class HistoricalUpgradeTests(TransactionTestCase):
    latest=('erp','0019_evidence_epochs_and_upload_origin')

    def tearDown(self):
        MigrationExecutor(connection).migrate([self.latest])
        super().tearDown()

    def test_preserves_contract_and_backfills_received_quantities_and_design_families(self):
        executor=MigrationExecutor(connection)
        old=('erp','0002_changerequest_requested_by')
        executor.migrate([old])
        apps=executor.loader.project_state([old]).apps
        user=apps.get_model('auth','User').objects.create(username='historical',password='!')
        org=apps.get_model('erp','Organization').objects.create(name='历史门店')
        common={'organization_id':org.pk,'owner_id':user.pk}
        customer=apps.get_model('erp','Customer').objects.create(**common,code='OLD-C',name='历史客户',phone='123')
        quote=apps.get_model('erp','Quote').objects.create(**common,code='OLD-Q',customer_id=customer.pk,title='历史报价',discount=Decimal('.9'),total=900)
        apps.get_model('erp','QuoteLine').objects.create(quote_id=quote.pk,name='原产品',quantity=2,unit_price=500)
        order=apps.get_model('erp','Order').objects.create(**common,code='OLD-O',customer_id=customer.pk,quote_id=quote.pk,title='历史变更订单',total=850)
        warehouse=apps.get_model('erp','Warehouse').objects.create(**common,code='OLD-W',name='历史仓')
        material=apps.get_model('erp','Material').objects.create(**common,code='OLD-M',name='历史物料')
        purchase=apps.get_model('erp','Purchase').objects.create(**common,code='OLD-P',supplier='历史供应商',warehouse_id=warehouse.pk,status='received')
        row=apps.get_model('erp','PurchaseLine').objects.create(purchase_id=purchase.pk,material_id=material.pk,quantity=10,unit_price=100)
        for code in ('OLD-D1','OLD-D2'):
            apps.get_model('erp','Design').objects.create(**common,code=code,customer_id=customer.pk,title=code)
        executor=MigrationExecutor(connection);executor.migrate([self.latest])
        from erp import models as m
        self.assertEqual(m.Order.objects.get(pk=order.pk).total,850)
        self.assertTrue(m.Order.objects.get(pk=order.pk).discount_confirmed)
        self.assertEqual(m.Order.objects.get(pk=order.pk).discount,Decimal('.9'))
        self.assertEqual(m.OrderLine.objects.get(order_id=order.pk).line_total,900)
        self.assertEqual(m.OrderLine.objects.get(order_id=order.pk).name,'原产品')
        self.assertEqual(m.PurchaseLine.objects.get(pk=row.pk).received_qty,10)
        self.assertEqual(m.StockMovement.objects.count(),0)
        self.assertEqual(len(set(m.Design.objects.values_list('family_id',flat=True))),2)

    def test_v11_pending_changes_and_factory_placeholders_upgrade_without_data_loss(self):
        executor=MigrationExecutor(connection);old=('erp','0007_backfill_execution_history');executor.migrate([old])
        apps=executor.loader.project_state([old]).apps
        user=apps.get_model('auth','User').objects.create(username='v11-history',password='!')
        Org=apps.get_model('erp','Organization');source=Org.objects.create(name='历史源店');factory=Org.objects.create(name='历史厂',kind='factory')
        Customer=apps.get_model('erp','Customer');Order=apps.get_model('erp','Order');Handoff=apps.get_model('erp','Handoff')
        customer=Customer.objects.create(code='V11-C',organization=source,owner=user,name='客户',phone='123')
        orders=[]
        for i in range(2):
            order=Order.objects.create(code=f'V11-S{i}',organization=source,owner=user,customer=customer,title='销售',total=1000,status='confirmed')
            handoff=Handoff.objects.create(code=f'V11-H{i}',organization=source,owner=user,order=order,target_organization=factory,payload={},status='accepted')
            partner=Customer.objects.create(code=f'V11-P{i}',organization=factory,owner=user,name='历史源店 · 生产协作',phone='内部协作',source='门厂交接')
            Order.objects.create(code=f'V11-F{i}',organization=factory,owner=user,customer=partner,title='工厂',total=0,kind='manufacturing',source_handoff=handoff,status='confirmed')
            orders.append(order)
        Change=apps.get_model('erp','ChangeRequest');Line=apps.get_model('erp','ChangeLine')
        for i,status in enumerate(['pending','approved']):
            change=Change.objects.create(code=f'V11-CH{i}',organization=source,owner=user,requested_by=user,order=orders[i],order_version=1,
                                         proposed_total=1500,proposed_due_date='2026-12-01',status=status,note='旧版增项')
            Line.objects.create(change=change,operation='add',name='旧增项',quantity=1,unit_price=500)
        MigrationExecutor(connection).migrate([self.latest])
        from erp import models as m
        self.assertEqual(m.Customer.objects.filter(is_internal=True).count(),2)
        self.assertEqual(m.Customer.objects.filter(internal_source_id=source.pk).count(),1)
        self.assertEqual(len(set(m.Order.objects.filter(kind='manufacturing').values_list('customer_id',flat=True))),1)
        self.assertEqual(m.Customer.objects.count(),3)
        self.assertEqual(m.ChangeRequest.objects.get(code='V11-CH0').status,'rejected')
        self.assertEqual(m.ChangeRequest.objects.get(code='V11-CH1').status,'approved')
        self.assertEqual(m.ChangeLine.objects.count(),2)
        self.assertTrue(all(value is None for value in m.ChangeLine.objects.values_list('net_amount',flat=True)))
        self.assertEqual(m.CollaborationRoute.objects.count(),0)
        self.assertFalse(m.Order.objects.filter(kind='sale',discount_confirmed=True).exists())
        self.assertFalse(m.Order.objects.filter(kind='manufacturing',discount_confirmed=False).exists())
        self.assertTrue(m.AuditEvent.objects.filter(action='upgrade_change_review').exists())

    def test_v112_archive_upgrade_preserves_old_file_and_sharing_metadata(self):
        executor=MigrationExecutor(connection);old=('erp','0015_backfill_contract_basis');executor.migrate([old])
        apps=executor.loader.project_state([old]).apps
        user=apps.get_model('auth','User').objects.create(username='archive-history',password='!')
        Org=apps.get_model('erp','Organization');source=Org.objects.create(name='旧门店');factory=Org.objects.create(name='旧工厂',kind='factory')
        common={'organization_id':source.pk,'owner_id':user.pk}
        customer=apps.get_model('erp','Customer').objects.create(**common,code='ARC-C',name='客户',phone='内部')
        order=apps.get_model('erp','Order').objects.create(**common,code='ARC-O',customer=customer,title='旧合同',total=1000)
        handoff=apps.get_model('erp','Handoff').objects.create(**common,code='ARC-H',order=order,target_organization=factory,payload={})
        item=apps.get_model('erp','Attachment').objects.create(**common,code='ARC-F',object_type='handoffs',object_id=handoff.pk,
                file='private/historical.pdf',original_name='历史图纸.pdf',size=12,sha256='a'*64)
        MigrationExecutor(connection).migrate([self.latest])
        from erp import models as m
        fresh=m.Attachment.objects.get(pk=item.pk)
        self.assertEqual(fresh.file.name,'private/historical.pdf');self.assertEqual(fresh.version,1)
        self.assertIsNone(fresh.archived_at);self.assertIsNone(fresh.deleted_at)
        self.assertEqual(m.Order.objects.get(pk=order.pk).total,1000)
        self.assertEqual(m.Handoff.objects.get(pk=handoff.pk).target_organization_id,factory.pk)

    def test_v114_archive_reason_is_backfilled_only_from_matching_audit(self):
        from django.utils import timezone
        old=('erp','0017_clarify_evidence_archive_label')
        executor=MigrationExecutor(connection);executor.migrate([old]);apps=executor.loader.project_state([old]).apps
        user=apps.get_model('auth','User').objects.create(username='reason-history',password='!')
        org=apps.get_model('erp','Organization').objects.create(name='归档历史店')
        common={'organization_id':org.pk,'owner_id':user.pk}
        Attachment=apps.get_model('erp','Attachment');Event=apps.get_model('erp','AuditEvent');at=timezone.now()
        items=[Attachment.objects.create(**common,code=f'CAUSE-{i}',object_type='handoffs',object_id=10,
              file=f'private/old-{i}.pdf',original_name='旧证据.pdf',size=12,sha256='a'*64,archived_at=at,version=3) for i in range(3)]
        Event.objects.create(**common,object_type='handoffs',object_id=10,code='H',action='stop_sharing_attachment',after={'attachment':items[0].code})
        Event.objects.create(**common,object_type='handoffs',object_id=10,code='H',action='archive_handoff_evidence',after={'attachments':[{'code':items[1].code}]})
        # The third archive has an event for a different parent: do not guess.
        Event.objects.create(**common,object_type='handoffs',object_id=99,code='OTHER',action='stop_sharing_attachment',after={'attachment':items[2].code})
        MigrationExecutor(connection).migrate([self.latest])
        from erp import models as m
        fresh=[m.Attachment.objects.get(pk=item.pk) for item in items]
        self.assertEqual([i.archive_reason for i in fresh],['stopped','reassigned',''])
        self.assertEqual([i.version for i in fresh],[3,3,3]);self.assertTrue(all(i.archived_at==at for i in fresh))
        self.assertEqual([i.file.name for i in fresh],[f'private/old-{i}.pdf' for i in range(3)])
        self.assertTrue(all(i.sha256=='a'*64 and i.deleted_at is None for i in fresh))
        self.assertTrue(all(i.uploaded_from_organization_id is None for i in fresh))
        self.assertEqual(m.EvidenceState.objects.count(),0)
