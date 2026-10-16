"""Executed on a real PostgreSQL service in CI; SQLite cannot prove row locking."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import skipUnless
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import connection,close_old_connections
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
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(compete,ids))
        self.assertCountEqual(results,['reserved','shortage'])
        stock=m.Stock.objects.get(warehouse=warehouse,material=material)
        self.assertEqual(stock.quantity,10)
        self.assertEqual(stock.reserved,6)
        self.assertEqual(m.StockReservation.objects.filter(status='active').count(),1)
