"""Executed on a real PostgreSQL service in CI; SQLite cannot prove row locking."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection,connections,close_old_connections
from django.test import TransactionTestCase,override_settings
from erp import models as m,services
from erp.services.materials import reserve_production


@skipUnless(connection.vendor=='postgresql','Real PostgreSQL row-lock verification')
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PostgreSQLContentionTests(TransactionTestCase):
    def test_two_jobs_cannot_reserve_same_available_stock(self):
        user=User.objects.create_superuser('lock-test',password='test-only')
        org=m.Organization.objects.create(name='并发工厂',kind='factory')
        common={'organization':org,'owner':user}
        customer=m.Customer.objects.create(**common,name='生产客户',phone='内部')
        warehouse=m.Warehouse.objects.create(**common,name='生产仓')
        material=m.Material.objects.create(**common,name='板材')
        services.adjust_stock(user,warehouse,material,10,'并发期初')
        ids=[]
        for _ in range(2):
            order=m.Order.objects.create(**common,customer=customer,title='生产',total=1000,status='confirmed')
            bom=m.Breakdown.objects.create(**common,order=order,status='released')
            m.BOMLine.objects.create(breakdown=bom,material=material,quantity=6)
            ids.append(m.Production.objects.create(**common,breakdown=bom,warehouse=warehouse,status='planned').pk)
        barrier=Barrier(2)
        def compete(pk):
            close_old_connections()
            try:
                actor=User.objects.get(pk=user.pk)
                job=m.Production.objects.get(pk=pk)
                barrier.wait(timeout=10)
                try:reserve_production(actor,job,job.version);return 'reserved'
                except ValidationError:return 'shortage'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(compete,ids))
        self.assertCountEqual(results,['reserved','shortage'])
        stock=m.Stock.objects.get(warehouse=warehouse,material=material)
        self.assertEqual(stock.quantity,10)
        self.assertEqual(stock.reserved,6)
        self.assertEqual(m.StockReservation.objects.filter(status='active').count(),1)


@skipUnless(connection.vendor=='postgresql','Real PostgreSQL business races')
@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class PostgreSQLBusinessRaces(TransactionTestCase):
    def setUp(self):
        self.actor=User.objects.create_superuser('race-admin',password='test-only')
        self.org=m.Organization.objects.create(name='竞争门店')
        self.common={'organization':self.org,'owner':self.actor}
        self.customer=m.Customer.objects.create(**self.common,name='测试客户',phone='内部')
        self.order=m.Order.objects.create(**self.common,customer=self.customer,title='测试订单',status='confirmed',total=1000,discount_confirmed=True)

    def race(self,calls):
        barrier=Barrier(len(calls))
        def run(call):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:call();return 'committed'
                except ValidationError:return 'rejected'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:results=list(pool.map(run,calls))
        self.assertCountEqual(results,['committed','rejected'])

    def test_concurrent_payments_cannot_exceed_contract_balance(self):
        from datetime import date
        def pay(reference):
            return lambda:services.record_payment(User.objects.get(pk=self.actor.pk),m.Order.objects.get(pk=self.order.pk),700,
                                                  reference,date(2026,10,16),'银行','')
        self.race([pay('RACE-PAY-1'),pay('RACE-PAY-2')])
        self.assertEqual(m.Payment.objects.count(),1)
        self.assertEqual(m.Payment.objects.get().amount,700)

    def test_cancel_and_bom_release_cannot_both_commit(self):
        material=m.Material.objects.create(**self.common,name='测试板材')
        bom=m.Breakdown.objects.create(**self.common,order=self.order,status='review')
        m.BOMLine.objects.create(breakdown=bom,material=material,quantity=1)
        def cancel():services.transition(User.objects.get(pk=self.actor.pk),m.Order.objects.get(pk=self.order.pk),'cancel',1,'客户取消')
        def release():services.transition(User.objects.get(pk=self.actor.pk),m.Breakdown.objects.get(pk=bom.pk),'release',1)
        self.race([cancel,release])
        self.order.refresh_from_db();bom.refresh_from_db()
        if self.order.status=='cancelled':
            self.assertEqual(bom.status,'review');self.assertFalse(m.Production.objects.exists())
        else:
            self.assertEqual(bom.status,'released');self.assertEqual(m.Production.objects.count(),1)

    def test_same_change_can_only_be_approved_once(self):
        from datetime import date
        approvers=[User.objects.create_superuser(f'approver-{i}',password='test-only') for i in range(2)]
        change=m.ChangeRequest.objects.create(**self.common,order=self.order,order_version=self.order.version,
                                             proposed_total=1100,proposed_due_date=date(2026,12,1),note='改单')
        def approve(pk):
            return lambda:services.transition(User.objects.get(pk=pk),m.ChangeRequest.objects.get(pk=change.pk),'approve',1)
        self.race([approve(user.pk) for user in approvers])
        self.order.refresh_from_db();self.assertEqual(self.order.total,1100)
        self.assertEqual(m.AuditEvent.objects.filter(object_type='changes',object_id=change.pk,action='approve').count(),1)

    def test_accept_and_withdraw_handoff_cannot_both_commit(self):
        from erp.services import collaboration
        factory=m.Organization.objects.create(name='竞争工厂',kind='factory')
        m.OrderLine.objects.create(order=self.order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        handoff=collaboration.send_handoff(self.actor,self.order,factory,self.order.version)
        def accept():collaboration.accept_handoff(User.objects.get(pk=self.actor.pk),m.Handoff.objects.get(pk=handoff.pk),1)
        def withdraw():services.transition(User.objects.get(pk=self.actor.pk),m.Handoff.objects.get(pk=handoff.pk),'withdraw',1,'客户暂缓')
        self.race([accept,withdraw])
        handoff.refresh_from_db()
        self.assertEqual(m.Order.objects.filter(source_handoff=handoff).count(),int(handoff.status=='accepted'))
        self.assertIn(handoff.status,['accepted','withdrawn'])

    def test_payment_and_lower_contract_revision_cannot_both_commit(self):
        from datetime import date
        from erp.services import receivables
        approver=User.objects.create_superuser('revision-approver',password='test-only')
        receivables.create_plan(self.actor,self.order,self.order.version,date(2026,10,16),[30,40,30])
        self.order.refresh_from_db()
        change=m.ChangeRequest.objects.create(**self.common,order=self.order,order_version=self.order.version,
                    proposed_total=600,proposed_due_date=date(2026,12,1),revise_payment_plan=True,note='减项')
        def approve():services.transition(User.objects.get(pk=approver.pk),m.ChangeRequest.objects.get(pk=change.pk),'approve',1)
        def pay():services.record_payment(User.objects.get(pk=self.actor.pk),m.Order.objects.get(pk=self.order.pk),700,
                                          'RACE-REVISION',date(2026,10,16),'银行','')
        self.race([approve,pay])
        self.order.refresh_from_db()
        paid=sum(m.Payment.objects.values_list('amount',flat=True))
        self.assertLessEqual(paid,self.order.total)
        self.assertEqual(sum(self.order.payment_plans.values_list('amount',flat=True)),self.order.total)

    def test_retarget_and_receiver_authorization_cannot_disclose_archived_file(self):
        from erp.services import collaboration,evidence
        from django.core.exceptions import PermissionDenied
        factory1=m.Organization.objects.create(name='旧目标',kind='factory')
        factory2=m.Organization.objects.create(name='新目标',kind='factory')
        receiver=User.objects.create_user('new-factory-only')
        m.Membership.objects.create(user=receiver,organization=factory2,role='planner',scope='org')
        m.OrderLine.objects.create(order=self.order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        handoff=collaboration.send_handoff(self.actor,self.order,factory1,self.order.version)
        item=m.Attachment.objects.create(**self.common,object_type='handoffs',object_id=handoff.pk,
              file='private/synthetic-no-file.pdf',original_name='旧厂图纸.pdf',kind='drawing',size=1,sha256='a'*64)
        services.transition(self.actor,handoff,'withdraw',handoff.version,'改派')
        handoff.refresh_from_db();self.order.refresh_from_db()
        barrier=Barrier(2)
        def run(which):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if which=='retarget':
                    collaboration.send_handoff(User.objects.get(pk=self.actor.pk),m.Order.objects.get(pk=self.order.pk),factory2,
                                               self.order.version,handoff_version=handoff.version)
                    return 'retargeted'
                try:evidence.authorized_attachment(User.objects.get(pk=receiver.pk),item.pk)
                except PermissionDenied:return 'blocked'
                return 'disclosed'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['retarget','read']))
        self.assertCountEqual(results,['retargeted','blocked'])
        item.refresh_from_db();self.assertIsNotNone(item.archived_at);self.assertIsNone(item.deleted_at)

    def test_stop_share_between_metadata_and_version_check_denies_receiver(self):
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import PermissionDenied
        from erp.services import collaboration,evidence
        factory=m.Organization.objects.create(name='停止共享工厂',kind='factory')
        receiver=User.objects.create_user('revoked-reader')
        m.Membership.objects.create(user=receiver,organization=factory,role='planner',scope='org')
        m.OrderLine.objects.create(order=self.order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        handoff=collaboration.send_handoff(self.actor,self.order,factory,self.order.version)
        item=m.Attachment.objects.create(**self.common,object_type='handoffs',object_id=handoff.pk,
               file='private/synthetic-revoke.pdf',original_name='共享图纸.pdf',size=1,sha256='b'*64)
        metadata_read=Event();revoked=Event();same_version=evidence._same_version
        def interleave(parent):
            metadata_read.set()
            if not revoked.wait(10):raise AssertionError('stop-share did not finish')
            return same_version(parent)
        def run(which):
            close_old_connections()
            try:
                if which=='write':
                    if not metadata_read.wait(10):raise AssertionError('reader did not reach version check')
                    evidence.stop_sharing_attachment(User.objects.get(pk=self.actor.pk),m.Attachment.objects.get(pk=item.pk),1,handoff.version,'误传')
                    revoked.set();return 'stopped'
                try:evidence.authorized_attachment(User.objects.get(pk=receiver.pk),item.pk)
                except PermissionDenied:return 'blocked'
                return 'disclosed'
            finally:connections.close_all()
        with patch('erp.services.evidence._same_version',side_effect=interleave):
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['read','write']))
        self.assertCountEqual(results,['blocked','stopped'])
        item.refresh_from_db();self.assertIsNotNone(item.archived_at)

    def test_delete_between_metadata_and_version_check_denies_receiver(self):
        from threading import Event
        from unittest.mock import patch
        from django.core.exceptions import PermissionDenied
        from erp.services import collaboration,evidence
        factory=m.Organization.objects.create(name='删除竞争工厂',kind='factory')
        receiver=User.objects.create_user('deleted-reader')
        m.Membership.objects.create(user=receiver,organization=factory,role='planner',scope='org')
        m.OrderLine.objects.create(order=self.order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        handoff=collaboration.send_handoff(self.actor,self.order,factory,self.order.version)
        item=m.Attachment.objects.create(**self.common,object_type='handoffs',object_id=handoff.pk,
               file='private/synthetic-delete.pdf',original_name='虚构图纸.pdf',size=1,sha256='c'*64)
        metadata_read=Event();deleted=Event();same_version=evidence._same_version
        def interleave(parent):
            metadata_read.set()
            if not deleted.wait(10):raise AssertionError('deletion did not finish')
            return same_version(parent)
        def run(which):
            close_old_connections()
            try:
                if which=='write':
                    if not metadata_read.wait(10):raise AssertionError('reader did not reach version check')
                    # Keep physical bytes deliberately outside this test: it
                    # proves authorization even before deferred purge executes.
                    with patch('erp.services.evidence.purge_attachment_file'):
                        evidence.delete_attachment(User.objects.get(pk=self.actor.pk),m.Attachment.objects.get(pk=item.pk),1)
                    deleted.set();return 'deleted'
                try:evidence.authorized_attachment(User.objects.get(pk=receiver.pk),item.pk)
                except PermissionDenied:return 'blocked'
                return 'disclosed'
            finally:connections.close_all()
        with patch('erp.services.evidence._same_version',side_effect=interleave):
            with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(run,['read','write']))
        self.assertCountEqual(results,['blocked','deleted'])
        item.refresh_from_db();handoff.refresh_from_db()
        self.assertIsNotNone(item.deleted_at);self.assertEqual(handoff.version,2)
