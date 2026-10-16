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
