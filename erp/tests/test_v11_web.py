import io
from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from erp import models as m
from .base import ERPTestCase


class UpgradeWebTests(ERPTestCase):
    def setUp(self):
        super().setUp(); self.client.force_login(self.admin)

    def test_new_feature_controls_and_printable_order(self):
        order=self.order(status='confirmed')
        m.OrderLine.objects.create(order=order,name='主卧衣柜',quantity=2,unit_price=500,line_total=1000)
        response=self.client.get(f'/modules/orders/{order.pk}/')
        self.assertContains(response,'主卧衣柜')
        self.assertContains(response,'私有附件')
        self.assertContains(response,'生产交接')
        print_page=self.client.get(f'/modules/orders/{order.pk}/print/')
        self.assertEqual(print_page.status_code,200)
        self.assertContains(print_page,'主卧衣柜')

    def test_design_new_revision_preserves_approved_version(self):
        design=self.make(m.Design,customer=self.customer,title='白橡方案',status='approved')
        response=self.client.post(f'/features/designs/{design.pk}/revision/',{'version':design.version})
        self.assertEqual(response.status_code,302)
        self.assertEqual(m.Design.objects.count(),2)
        self.assertEqual(m.Design.objects.get(previous_version=design).revision,2)
        design.refresh_from_db(); self.assertEqual(design.status,'approved')

    def test_csv_import_rolls_back_whole_file_on_bad_row(self):
        content='name,phone,address,source\n张先生,13800001111,杭州,介绍\n坏数据,,杭州,介绍\n'
        response=self.client.post('/imports/customers/',{'organization':self.org.pk,'file':SimpleUploadedFile('客户.csv',content.encode('utf-8'))})
        self.assertEqual(response.status_code,200)
        self.assertFalse(m.Customer.objects.filter(name='张先生').exists())
        response=self.client.post('/imports/customers/',{'organization':self.org.pk,'file':SimpleUploadedFile('客户.csv',content.split('坏数据')[0].encode('utf-8'))})
        self.assertEqual(response.status_code,302)
        self.assertTrue(m.Customer.objects.filter(name='张先生').exists())

    def test_supplier_and_followup_are_live_scoped_modules(self):
        for key in ['suppliers','followups','handoffs','receipts']:
            self.assertEqual(self.client.get(f'/modules/{key}/').status_code,200)
        self.client.post('/modules/followups/new/',{'organization':self.org.pk,'owner':self.admin.pk,'customer':self.customer.pk,
                                                 'channel':'电话','note':'确认材质，下周复尺','next_date':'2026-11-01'})
        self.assertEqual(m.FollowUp.objects.count(),1)

    def test_list_size_and_date_filters_validate_inputs(self):
        for i in range(20): self.make(m.Customer,name=f'客户{i}',phone='123')
        response=self.client.get('/modules/customers/?page_size=10&sort=name')
        self.assertEqual(len(response.context['page_obj'].object_list),10)
        self.assertEqual(self.client.get('/modules/customers/?date_from=bad&sort=__malicious&page_size=bad').status_code,200)
