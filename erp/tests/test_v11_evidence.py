from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import override_settings
from tempfile import TemporaryDirectory
from erp import models as m
from erp.services import evidence, collaboration
from .base import ERPTestCase


class EvidenceCollaborationTests(ERPTestCase):
    def test_private_attachment_checks_parent_and_signature(self):
        self.assertTrue(hasattr(evidence,'upload_attachment'),'Private attachments must be implemented')
        with TemporaryDirectory() as folder, override_settings(MEDIA_ROOT=folder):
            file = SimpleUploadedFile('量尺.pdf',b'%PDF-1.7\nfixture')
            item = evidence.upload_attachment(self.admin,self.customer,file,'drawing')
            self.assertEqual(item.original_name,'量尺.pdf')
            outsider = User.objects.create_user('outsider')
            with self.assertRaises(PermissionDenied):
                evidence.authorized_attachment(outsider,item.pk)
            with self.assertRaises(ValidationError):
                evidence.upload_attachment(self.admin,self.customer,SimpleUploadedFile('evil.png',b'<html>fake</html>'),'photo')

    def test_factory_handoff_redacts_retail_data_and_uses_local_order(self):
        self.assertTrue(hasattr(collaboration,'send_handoff'),'Factory handoff must be implemented')
        factory = m.Organization.objects.create(name='定制工厂',kind='factory')
        m.CollaborationRoute.objects.create(source=self.org,target=factory)
        planner = User.objects.create_user('factory_planner')
        m.Membership.objects.create(user=planner,organization=factory,role='planner',scope='org')
        order = self.order(status='confirmed')
        m.OrderLine.objects.create(order=order,name='白橡衣柜',quantity=1,unit_price=1000,line_total=1000,unit_cost=600)
        handoff = collaboration.send_handoff(self.manager,order,factory,order.version,'按已确认规格生产')
        self.assertNotIn('unit_price',str(handoff.payload))
        self.assertNotIn(self.customer.phone,str(handoff.payload))
        local = collaboration.accept_handoff(planner,handoff,handoff.version)
        self.assertEqual(local.organization,factory)
        self.assertEqual(local.kind,'manufacturing')
        self.assertEqual(local.lines.get().name,'白橡衣柜')
        self.assertEqual(local.lines.get().unit_price,0)
        self.assertEqual(local.total,0)
        from erp.permissions import scoped
        self.assertFalse(scoped(planner,m.Order).filter(pk=order.pk).exists())
        with self.assertRaises(ValidationError):
            collaboration.accept_handoff(planner,handoff,handoff.version)

    def test_attachment_download_rechecks_assignment_and_has_no_public_url(self):
        from django.test import Client
        from erp import services
        customer=self.make(m.Customer,name='负责客户',phone='123',owner=self.sales)
        membership=m.Membership.objects.get(user=self.sales);membership.scope='own';membership.save()
        with TemporaryDirectory() as folder,override_settings(MEDIA_ROOT=folder):
            file=SimpleUploadedFile('证据.pdf',b'%PDF-1.7\nfixture')
            item=evidence.upload_attachment(self.sales,customer,file,'drawing')
            self.client.force_login(self.sales)
            response=self.client.get(f'/attachments/{item.pk}/download/')
            self.assertEqual(response.status_code,200)
            self.assertEqual(b''.join(response.streaming_content),b'%PDF-1.7\nfixture')
            self.assertEqual(response['Cache-Control'],'private, no-store')
            self.assertEqual(Client().get(f'/attachments/{item.pk}/download/').status_code,302)
            self.assertEqual(self.client.get('/media/'+item.file.name).status_code,404)
            customer.owner=self.admin;customer.save()
            self.assertEqual(self.client.get(f'/attachments/{item.pk}/download/').status_code,403)
            oversized=SimpleUploadedFile('big.pdf',b'%PDF-1.7');oversized.size=evidence.MAX_BYTES+1
            with self.assertRaises(ValidationError):evidence.upload_attachment(self.admin,customer,oversized,'drawing')

    def test_factory_order_cannot_cancel_or_change_frozen_handoff_products(self):
        from erp import services
        from datetime import date
        factory=m.Organization.objects.create(name='冻结工厂',kind='factory')
        order=self.order(status='confirmed')
        m.OrderLine.objects.create(order=order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        handoff=collaboration.send_handoff(self.admin,order,factory,order.version)
        local=collaboration.accept_handoff(self.admin,handoff,handoff.version)
        with self.assertRaises(ValidationError):services.transition(self.admin,local,'cancel',local.version,'不能丢失已接单')
        change=m.ChangeRequest.objects.create(organization=factory,owner=self.admin,order=local,order_version=local.version,
                                              proposed_total=100,proposed_due_date=date(2026,12,1),note='不得旁路修改')
        factory_manager=User.objects.create_user('factory_manager')
        m.Membership.objects.create(user=factory_manager,organization=factory,role='manager',scope='org')
        with self.assertRaises(ValidationError):services.transition(factory_manager,change,'approve',change.version)
