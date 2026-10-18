"""Seventh-review business/evidence boundaries and scoped governance."""
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from erp import models as m,services
from erp.services import evidence,collaboration,pricing
from .base import ERPTestCase


class EvidenceBoundaryTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=self.enterContext(TemporaryDirectory())))
        self.factory=m.Organization.objects.create(name='七次评审工厂',kind='factory')
        self.receiver=User.objects.create_user('epoch-receiver')
        self.factory_manager=User.objects.create_user('epoch-factory-manager')
        for user,role in ((self.receiver,'planner'),(self.factory_manager,'manager')):
            m.Membership.objects.create(user=user,organization=self.factory,role=role,scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.source=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.source,name='原产品',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version)

    def upload(self,user,parent=None):
        parent=parent or self.handoff;parent.refresh_from_db()
        return evidence.upload_attachment(user,parent,SimpleUploadedFile('虚构图纸.pdf',b'%PDF-1.7 synthetic epoch fixture'),
             'drawing',True,parent.version,self.factory.pk)

    def stop(self,user,item):
        self.handoff.refresh_from_db()
        return evidence.stop_sharing_attachment(user,item,item.version,self.handoff.version,'七次评审止损')

    def test_deleting_contract_evidence_does_not_invalidate_pending_approval(self):
        order=self.order(status='confirmed',owner=self.sales)
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,requested_by=self.sales,
                         proposed_total=1000,proposed_due_date=date(2026,12,1),note='仅改交期')
        item=self.upload(self.admin,order);order.refresh_from_db();version=order.version;at=order.updated_at
        evidence.delete_attachment(self.admin,item,item.version)
        order.refresh_from_db();self.assertEqual(order.version,version);self.assertEqual(order.updated_at,at)
        services.transition(self.manager,change,'approve',change.version)

    def test_attachment_mutations_never_full_clean_or_save_parent(self):
        item=self.upload(self.sales,self.source)
        with patch.object(m.Order,'full_clean',wraps=self.source.full_clean) as clean,patch.object(m.Order,'save',wraps=self.source.save) as save:
            evidence.delete_attachment(self.sales,item,item.version)
        self.assertEqual(clean.call_count,0);self.assertEqual(save.call_count,0)

    def test_manual_stop_only_changes_evidence_not_handoff_business_version(self):
        item=self.upload(self.receiver);version=self.handoff.version;at=self.handoff.updated_at
        self.stop(self.manager,item);self.handoff.refresh_from_db()
        self.assertEqual(self.handoff.version,version);self.assertEqual(self.handoff.updated_at,at)

    def test_each_collection_mutation_advances_an_independent_epoch(self):
        epoch=getattr(evidence,'evidence_epoch',lambda parent:0)
        before=epoch(self.handoff);item=self.upload(self.sales)
        self.assertEqual(epoch(self.handoff),before+1)
        self.stop(self.manager,item);self.assertEqual(epoch(self.handoff),before+2)
        item.refresh_from_db();evidence.delete_attachment(self.sales,item,item.version)
        self.assertEqual(epoch(self.handoff),before+3)

    def test_disabled_route_blocks_source_and_factory_upload_but_keeps_source_governance(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        for user in (self.sales,self.manager,self.receiver,self.factory_manager):
            with self.subTest(user=user.username):
                with self.assertRaises(PermissionDenied):self.upload(user)
        self.stop(self.manager,item)
        self.assertEqual(evidence.authorized_attachment(self.manager,item.pk).pk,item.pk)

    def test_factory_uploader_can_stop_own_file_even_after_route_disabled(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        self.stop(self.receiver,item);item.refresh_from_db();self.assertIsNotNone(item.archived_at)
        with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)

    def test_factory_management_can_only_stop_proven_current_factory_uploads(self):
        source=self.upload(self.sales);factory=self.upload(self.receiver)
        self.assertEqual(getattr(source,'uploaded_from_organization_id',None),self.org.pk)
        self.assertEqual(getattr(factory,'uploaded_from_organization_id',None),self.factory.pk)
        self.route.enabled=False;self.route.save()
        with self.assertRaises(PermissionDenied):self.stop(self.factory_manager,source)
        self.stop(self.factory_manager,factory)
        with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.factory_manager,source,source.version)

    def test_factory_management_cannot_borrow_another_roles_scope(self):
        item=self.upload(self.receiver)
        member=m.Membership.objects.get(user=self.factory_manager);member.scope='own';member.save()
        m.Membership.objects.create(user=self.factory_manager,organization=self.factory,role='planner',scope='org')
        with self.assertRaises(PermissionDenied):self.stop(self.factory_manager,item)
        own=self.upload(self.factory_manager);self.route.enabled=False;self.route.save()
        self.stop(self.factory_manager,own)

    def test_legacy_unknown_origin_does_not_give_factory_managers_governance(self):
        item=self.make(m.Attachment,owner=self.receiver,object_type='handoffs',object_id=self.handoff.pk,
                 file='private/unknown.pdf',original_name='历史来源未分类.pdf',size=1,sha256='a'*64)
        with self.assertRaises(PermissionDenied):self.stop(self.factory_manager,item)
        self.route.enabled=False;self.route.save()
        self.stop(self.receiver,item)

    def test_real_business_change_still_invalidates_approval(self):
        order=self.order(status='confirmed')
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,requested_by=self.sales,
                         proposed_total=1000,proposed_due_date=date(2026,12,1),note='待核对')
        order.version+=1;order.save()
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
