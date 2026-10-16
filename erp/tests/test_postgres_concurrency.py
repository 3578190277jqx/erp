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
        self.order=m.Order.objects.create(**self.common,customer=self.customer,title='测试订单',status='confirmed',total=1000)

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
