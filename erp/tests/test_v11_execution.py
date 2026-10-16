from datetime import date, timedelta
from decimal import Decimal
from django.core.exceptions import ValidationError
from erp import models as m, services
from erp.services import materials, receivables
from .base import ERPTestCase


class ExecutionUpgradeTests(ERPTestCase):
    def production(self):
        order = self.order(status='confirmed')
        bom = self.make(m.Breakdown,order=order,status='released')
        m.BOMLine.objects.create(breakdown=bom,material=self.board,quantity=6)
        return self.make(m.Production,breakdown=bom,warehouse=self.warehouse,status='planned')

    def test_reservation_protects_other_jobs_and_transfers_to_issue(self):
        self.assertTrue(hasattr(materials,'reserve_production'),'Reservations must be implemented')
        services.adjust_stock(self.admin,self.warehouse,self.board,10,'期初')
        a,b = self.production(),self.production()
        materials.reserve_production(self.admin,a,a.version)
        with self.assertRaises(ValidationError):
            materials.reserve_production(self.admin,b,b.version)
        stock = m.Stock.objects.get(material=self.board)
        self.assertEqual(stock.reserved,Decimal(6))
        with self.assertRaises(ValidationError):
            services.adjust_stock(self.admin,self.warehouse,self.board,-5,'不允许抢用预留')
        a.refresh_from_db()
        services.transition(self.admin,a,'issue',a.version)
        stock.refresh_from_db()
        self.assertEqual(stock.quantity,Decimal(4))
        self.assertEqual(stock.reserved,Decimal(0))

    def test_partial_receipt_is_atomic_and_never_overreceives(self):
        self.assertTrue(hasattr(materials,'receive_partial'),'Partial receipts must be implemented')
        purchase = self.make(m.Purchase,supplier='板材厂',warehouse=self.warehouse,status='ordered')
        row = m.PurchaseLine.objects.create(purchase=purchase,material=self.board,quantity=10,unit_price=100)
        materials.receive_partial(self.admin,purchase,{row.pk:Decimal(4)},'RC-001',purchase.version)
        purchase.refresh_from_db(); row.refresh_from_db()
        self.assertEqual(purchase.status,'partial')
        self.assertEqual(row.received_qty,Decimal(4))
        with self.assertRaises(ValidationError):
            materials.receive_partial(self.admin,purchase,{row.pk:7},'RC-002',purchase.version)
        self.assertEqual(m.Stock.objects.get(material=self.board).quantity,Decimal(4))
        services.transition(self.admin,purchase,'receive',purchase.version)
        row.refresh_from_db(); purchase.refresh_from_db()
        self.assertEqual(row.received_qty,Decimal(10))
        self.assertEqual(purchase.status,'received')

    def test_installments_have_no_rounding_gap_and_gate_production(self):
        self.assertTrue(hasattr(receivables,'create_plan'),'Installments must be implemented')
        order=self.order(status='confirmed')
        receivables.create_plan(self.manager,order,order.version,date.today(),[30,40,30])
        self.assertEqual(list(order.payment_plans.values_list('amount',flat=True)),[Decimal(300),Decimal(400),Decimal(300)])
        with self.assertRaises(ValidationError):
            receivables.check_gate(order,'start')
        services.record_payment(self.finance,order,300,'PLAN-001',date.today(),'银行','')
        receivables.check_gate(order,'start')
        with self.assertRaises(ValidationError):
            receivables.check_gate(order,'ship')
        self.assertEqual(receivables.plan_rows(order)[0]['paid'],Decimal(300))
        order2 = self.make(m.Order,customer=self.customer,title='尾差',total=Decimal('1000.01'),status='confirmed')
        receivables.create_plan(self.manager,order2,order2.version,date.today(),[33,33,34])
        self.assertEqual(sum(order2.payment_plans.values_list('amount',flat=True)),Decimal('1000.01'))

    def test_reserved_job_cannot_switch_warehouse_through_edit_form(self):
        services.adjust_stock(self.admin,self.warehouse,self.board,10,'期初')
        production=self.production()
        materials.reserve_production(self.admin,production,production.version)
        production.refresh_from_db()
        other=self.make(m.Warehouse,name='另一个仓库')
        self.client.force_login(self.admin)
        response=self.client.post(f'/modules/production/{production.pk}/edit/',{'version':production.version,'owner':self.admin.pk,
                                  'warehouse':other.pk,'work_center':'一线','start_date':'2026-11-01','due_date':'2026-11-10','note':''})
        self.assertEqual(response.status_code,200)
        production.refresh_from_db();self.assertEqual(production.warehouse_id,self.warehouse.pk)

    def test_draft_plan_cannot_freeze_a_smaller_contract_amount(self):
        order=self.order(status='draft')
        with self.assertRaises(ValidationError):receivables.create_plan(self.manager,order,order.version,date.today(),[30,40,30])
        self.assertFalse(order.payment_plans.exists())
        order.status='confirmed';order.save()
        receivables.create_plan(self.manager,order,order.version,date.today(),[30,40,30])
        m.Order.objects.filter(pk=order.pk).update(total=2000)
        order.refresh_from_db()
        with self.assertRaises(ValidationError):receivables.check_gate(order,'start')
