from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied,ValidationError
from erp import models as m,services
from erp.services import collaboration,receivables,pricing
from .base import ERPTestCase


class RecoveryTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.factory=m.Organization.objects.create(name='接单工厂',kind='factory')
        self.planner=User.objects.create_user('receiver')
        m.Membership.objects.create(user=self.planner,organization=self.factory,role='planner',scope='org')
        self.route=m.CollaborationRoute.objects.create(source=self.org,target=self.factory)

    def sent(self):
        order=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=order,name='柜体',quantity=1,unit_price=1000,line_total=1000)
        return order,collaboration.send_handoff(self.sales,order,self.factory,order.version)

    def test_source_can_withdraw_sent_and_cancel_without_factory_reply(self):
        order,handoff=self.sent()
        self.assertIn('withdraw',dict(services.available_actions(handoff)))
        services.transition(self.sales,handoff,'withdraw',handoff.version,'客户暂缓')
        handoff.refresh_from_db();order.refresh_from_db()
        self.assertEqual(handoff.status,'withdrawn')
        services.transition(self.sales,order,'cancel',order.version,'撤单')
        self.assertFalse(m.Order.objects.filter(source_handoff=handoff).exists())
        self.assertTrue(m.AuditEvent.objects.filter(action='withdraw_handoff').exists())

    def test_disabled_route_still_allows_source_withdraw_and_later_resend(self):
        order,handoff=self.sent();self.route.enabled=False;self.route.save()
        services.transition(self.sales,handoff,'withdraw',handoff.version,'合作授权停用')
        order.refresh_from_db();handoff.refresh_from_db()
        self.route.enabled=True;self.route.save()
        sent=collaboration.send_handoff(self.sales,order,self.factory,order.version,handoff_version=handoff.version)
        self.assertEqual(sent.status,'sent')
        with self.assertRaises(ValidationError):services.transition(self.sales,sent,'withdraw',1,'旧版本')

    def test_factory_cannot_withdraw_source_order_or_accept_after_withdrawal(self):
        order,handoff=self.sent()
        with self.assertRaises(PermissionDenied):services.transition(self.planner,handoff,'withdraw',handoff.version,'越权')
        services.transition(self.sales,handoff,'withdraw',handoff.version,'客户暂缓')
        with self.assertRaises((ValidationError,PermissionDenied)):collaboration.accept_handoff(self.planner,handoff,1)
        from erp.permissions import scoped
        self.assertFalse(scoped(self.planner,m.Handoff).filter(pk=handoff.pk).exists())

    def test_generic_handoff_http_actions_use_source_and_factory_permissions(self):
        order,handoff=self.sent()
        self.client.force_login(self.sales)
        self.assertContains(self.client.get(f'/modules/handoffs/{handoff.pk}/'),'撤回交接')
        self.client.post(f'/modules/handoffs/{handoff.pk}/action/',{'action':'withdraw','version':handoff.version,'note':'暂缓'})
        handoff.refresh_from_db();self.assertEqual(handoff.status,'withdrawn')

    def test_real_requester_can_withdraw_pending_change(self):
        order=self.order(status='confirmed')
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=1000,
                         proposed_due_date=order.due_date,note='客户改单',owner=self.sales)
        with self.assertRaises((PermissionDenied,ValidationError)):services.transition(self.manager,change,'withdraw',change.version,'非申请人')
        services.transition(self.sales,change,'withdraw',change.version,'客户不再变更')
        change.refresh_from_db();self.assertEqual(change.status,'withdrawn')
        with self.assertRaises(ValidationError):services.transition(self.manager,change,'approve',change.version)
