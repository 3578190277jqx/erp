"""Sixth-review evidence lifecycle and approval consistency regressions."""
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import F
from django.test import override_settings
from erp import models as m,services
from erp.services import collaboration,evidence,pricing
from .base import ERPTestCase


class LifecycleTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(MEDIA_ROOT=self.enterContext(TemporaryDirectory())))
        self.factory=m.Organization.objects.create(name='生命周期验收工厂',kind='factory')
        self.receiver=User.objects.create_user('lifecycle-receiver')
        m.Membership.objects.create(user=self.receiver,organization=self.factory,role='planner',scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        self.source=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=self.source,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        self.handoff=collaboration.send_handoff(self.sales,self.source,self.factory,self.source.version)

    def upload(self,user):
        self.handoff.refresh_from_db()
        return evidence.upload_attachment(user,self.handoff,SimpleUploadedFile('生产资料.pdf',b'%PDF-1.7 fictional evidence'),
                  'drawing',True,self.handoff.version,self.factory.pk)

    def test_disabled_route_blocks_all_receiver_writes_but_preserves_read(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        for status in ('sent','accepted','completed'):
            self.handoff.status=status;self.handoff.save(update_fields=['status'])
            with self.subTest(status=status):
                self.assertFalse(evidence.can_write_evidence(self.receiver,self.handoff))
                self.assertEqual(evidence.authorized_attachment(self.receiver,item.pk).pk,item.pk)
                with self.assertRaises(PermissionDenied):self.upload(self.receiver)
                with self.assertRaises(PermissionDenied):evidence.delete_attachment(self.receiver,item,item.version)
                with self.assertRaises(PermissionDenied):evidence.stop_sharing_attachment(self.receiver,item,item.version,self.handoff.version,'撤回')
        item.refresh_from_db();self.assertIsNone(item.archived_at);self.assertIsNone(item.deleted_at)
        self.client.force_login(self.receiver)
        page=self.client.get(f'/modules/handoffs/{self.handoff.pk}/')
        self.assertFalse(page.context['can_attach'])
        self.assertNotContains(page,'type="file" name="file"');self.assertNotContains(page,'撤回我的文件')

    def test_disabled_route_keeps_source_governance_and_factory_rejection(self):
        item=self.upload(self.receiver);self.route.enabled=False;self.route.save()
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'停止误传')
        self.upload(self.sales)
        self.handoff.refresh_from_db()
        with self.assertRaises(PermissionDenied):collaboration.accept_handoff(self.receiver,self.handoff,self.handoff.version)
        collaboration.accept_handoff(self.receiver,self.handoff,self.handoff.version,True,'协作已停用，拒绝在途单')
        self.handoff.refresh_from_db();self.assertEqual(self.handoff.status,'rejected')

    def test_manual_archive_cause_and_role_specific_labels(self):
        item=self.upload(self.sales);self.upload(self.receiver)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),'停止向工厂共享')
        self.client.force_login(self.receiver)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),'撤回我的文件')
        evidence.stop_sharing_attachment(self.manager,item,item.version,self.handoff.version,'误传')
        item.refresh_from_db();self.assertEqual(getattr(item,'archive_reason',''),'stopped')
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(f'/modules/handoffs/{self.handoff.pk}/'),'人工停止共享 · 仅源门店查阅')

    def test_reassignment_records_a_distinct_archive_cause(self):
        item=self.upload(self.receiver)
        services.transition(self.manager,self.handoff,'withdraw',self.handoff.version,'改派')
        self.handoff.refresh_from_db();self.source.refresh_from_db()
        target=m.Organization.objects.create(name='新接单厂',kind='factory')
        m.CollaborationRoute.objects.create(source=self.org,target=target)
        collaboration.send_handoff(self.manager,self.source,target,self.source.version,handoff_version=self.handoff.version)
        item.refresh_from_db();self.assertEqual(getattr(item,'archive_reason',''),'reassigned')
        self.assertTrue(item.file.storage.exists(item.file.name));self.assertIsNone(item.deleted_at)

    def test_deletion_advances_parent_version(self):
        item=self.upload(self.sales);old=self.handoff.version
        evidence.delete_attachment(self.sales,item,item.version)
        self.handoff.refresh_from_db();self.assertEqual(self.handoff.version,old+1)
        self.assertTrue(m.AuditEvent.objects.filter(action='delete_attachment').exists())

    def test_deletion_after_metadata_read_denies_download(self):
        item=self.upload(self.sales);same=evidence._same_version;deleted=False
        def delete_before_check(parent):
            nonlocal deleted
            if not deleted:
                deleted=True;evidence.delete_attachment(self.sales,item,item.version)
            return same(parent)
        with patch('erp.services.evidence._same_version',side_effect=delete_before_check):
            with self.assertRaises(PermissionDenied):evidence.authorized_attachment(self.receiver,item.pk)

    def test_download_retries_successfully_after_a_harmless_version_change(self):
        item=self.upload(self.sales);same=evidence._same_version;checks=0
        def change_once(parent):
            nonlocal checks
            checks+=1
            if checks==1:m.Handoff.objects.filter(pk=parent.pk).update(version=F('version')+1)
            return same(parent)
        with patch('erp.services.evidence._same_version',side_effect=change_once):
            self.assertEqual(evidence.authorized_attachment(self.receiver,item.pk).pk,item.pk)
        self.assertEqual(checks,2)

    def test_detail_never_mixes_new_parent_version_into_old_page(self):
        self.upload(self.sales)
        m.Handoff.objects.filter(pk=self.handoff.pk).update(version=F('version')+1)
        files,rights,stale=evidence.attachment_context(self.receiver,self.handoff)
        self.assertEqual(files,[]);self.assertFalse(rights.visible);self.assertTrue(stale)
