import io
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from django.core.exceptions import ValidationError,PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from erp import models as m,services
from erp.services import receivables,evidence,daily,collaboration
from .base import ERPTestCase


class SecondReviewFinanceTests(ERPTestCase):
    def planned(self):
        order=self.order(status='confirmed')
        receivables.create_plan(self.manager,order,order.version,date(2026,10,20),[30,40,30])
        order.refresh_from_db();return order

    def test_tail_first_does_not_clear_deposit_gate(self):
        order=self.planned();tail=order.payment_plans.get(sequence=3)
        services.record_payment(self.finance,order,300,'TAIL-FIRST',date.today(),'银行','',plan=tail)
        rows=receivables.plan_rows(order)
        self.assertEqual(rows[0]['paid'],0);self.assertEqual(rows[2]['paid'],300)
        with self.assertRaises(ValidationError):receivables.check_gate(order,'start')
        services.record_payment(self.finance,order,700,'BALANCE-FIFO',date.today(),'银行','')
        receivables.check_gate(order,'start');receivables.check_gate(order,'ship');receivables.check_gate(order,'complete')
        self.assertTrue(all(row['balance']==0 for row in receivables.plan_rows(order)))

    def test_period_cannot_overpay_or_use_another_order(self):
        order=self.planned();other=self.order(status='confirmed')
        plan=order.payment_plans.first()
        with self.assertRaises(ValidationError):services.record_payment(self.finance,other,100,'WRONG-PERIOD',date.today(),'银行','',plan=plan)
        with self.assertRaises(ValidationError):services.record_payment(self.finance,order,301,'OVER-PERIOD',date.today(),'银行','',plan=plan)
        self.assertEqual(m.Payment.objects.count(),0)

    def test_delivery_date_change_reschedules_auto_periods_and_audits(self):
        order=self.planned();before=list(order.payment_plans.values_list('due_date',flat=True))
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=order.total,
                         proposed_due_date=date(2026,12,30),note='延期交付')
        services.transition(self.manager,change,'approve',change.version)
        self.assertEqual(list(order.payment_plans.values_list('due_date',flat=True)),[before[0],date(2026,12,23),date(2026,12,30)])
        self.assertTrue(m.AuditEvent.objects.filter(action='reschedule_payment_plan').exists())

    def test_org_can_require_plan_without_changing_legacy_default(self):
        order=self.order(status='confirmed')
        receivables.check_gate(order,'start')
        self.org.require_payment_plan=True;self.org.save()
        order.refresh_from_db()
        with self.assertRaises(ValidationError):receivables.check_gate(order,'start')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(f'/modules/orders/{order.pk}/'),'本组织要求分期计划')

    def test_http_period_choices_and_payment_reject_cross_order(self):
        order=self.planned();plan=order.payment_plans.get(sequence=3)
        self.client.force_login(self.finance)
        response=self.client.get(f'/finance/order-plans/?order={order.pk}')
        self.assertEqual(response.status_code,200)
        self.assertIn(plan.pk,[row['id'] for row in response.json()['plans']])
        payload={'direction':'in','order':order.pk,'plan':plan.pk,'amount':300,'reference':'WEB-TAIL',
                 'paid_date':'2026-10-16','method':'银行','note':''}
        self.assertEqual(self.client.post('/modules/finance/new/',payload).status_code,302)
        self.assertEqual(m.Payment.objects.get(reference='WEB-TAIL').plan_id,plan.pk)
        self.client.force_login(self.sales)
        self.assertEqual(self.client.get(f'/finance/order-plans/?order={order.pk}').status_code,403)


class SecondReviewDailyTests(ERPTestCase):
    def test_upload_reads_bounded_chunks(self):
        class BoundedFile(SimpleUploadedFile):
            def read(self,size=-1):
                if size<0 or size>65536:raise AssertionError('Unbounded upload read')
                return super().read(size)
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            payload=b'%PDF-1.7\n'+b'x'*200000
            file=BoundedFile('bounded.pdf',payload)
            item=evidence.upload_attachment(self.admin,self.customer,file,'drawing')
            self.assertEqual(item.size,len(payload))

    def test_delete_checks_parent_and_version_then_cleans_after_commit(self):
        self.assertTrue(hasattr(evidence,'delete_attachment'))
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            item=evidence.upload_attachment(self.admin,self.customer,SimpleUploadedFile('proof.pdf',b'%PDF-1.7'),'drawing')
            name=item.file.path
            with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.buyer,item,item.version)
            with self.assertRaises(ValidationError):evidence.delete_attachment(self.admin,item,0)
            with self.captureOnCommitCallbacks(execute=True):evidence.delete_attachment(self.admin,item,item.version)
            self.assertFalse(Path(name).exists())
            with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.admin,item.pk)
            item.refresh_from_db();self.assertIsNotNone(item.deleted_at)
            self.assertTrue(m.AuditEvent.objects.filter(action='delete_attachment').exists())

    def test_duplicate_csv_reimport_and_same_file_duplicate_roll_back(self):
        content='name,specification,unit,unit_cost,safety_stock\n新木板,18mm,张,100,10\n'
        daily.import_records(self.admin,'materials',self.org,SimpleUploadedFile('materials.csv',content.encode()))
        with self.assertRaises(ValidationError):daily.import_records(self.admin,'materials',self.org,SimpleUploadedFile('materials.csv',content.encode()))
        self.assertEqual(m.Material.objects.filter(name='新木板').count(),1)
        content='name,phone,address,source\n导入甲,1234567,杭州,介绍\n导入乙,1234567,杭州,介绍\n'
        with self.assertRaises(ValidationError):daily.import_records(self.admin,'customers',self.org,SimpleUploadedFile('customers.csv',content.encode()))
        self.assertFalse(m.Customer.objects.filter(name='导入甲').exists())

    def test_progress_rejects_other_legal_order_states(self):
        order=self.order(status='confirmed')
        with self.assertRaises(ValidationError):collaboration.return_progress(self.admin,order,'completed')

    def test_private_delete_http_and_cad_signature_boundaries(self):
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            item=evidence.upload_attachment(self.admin,self.customer,SimpleUploadedFile('proof.pdf',b'%PDF-1.7'),'drawing')
            self.client.force_login(self.admin)
            self.assertContains(self.client.get(f'/modules/customers/{self.customer.pk}/'),'删除')
            with self.captureOnCommitCallbacks(execute=True):
                response=self.client.post(f'/attachments/{item.pk}/delete/',{'version':item.version})
            self.assertEqual(response.status_code,302)
            self.assertEqual(self.client.get(f'/attachments/{item.pk}/download/').status_code,403)
            self.assertFalse(evidence.valid_signature('.dxf',b'this is SECTION but not DXF'))
            self.assertTrue(evidence.valid_signature('.dxf',b'0\r\nSECTION\r\n2\r\nHEADER'))
            self.assertFalse(evidence.valid_signature('.dwg',b'AC10html'))
            self.assertTrue(evidence.valid_signature('.dwg',b'AC1027\x00'))
