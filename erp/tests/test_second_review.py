from datetime import date
from decimal import Decimal
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from erp import models as m, services
from erp.services import collaboration, pricing
from .base import ERPTestCase


class SecondReviewPricingTests(ERPTestCase):
    def priced_order(self):
        quote=self.make(m.Quote,customer=self.customer,title='保留计价',discount=Decimal('.95'),tax_rate=Decimal('.13'))
        services.add_line(self.admin,quote,dict(name='衣柜',specification='白色',pricing_mode='projected',width_mm=3200,height_mm=2600,
                          quantity=1,unit='㎡',unit_price=1380,unit_cost=760),quote.version)
        quote.refresh_from_db()
        for action in ('submit','approve','accept'):
            services.transition(self.admin,quote,action,quote.version);quote.refresh_from_db()
        order=m.Order.objects.get(quote=quote);order.status='confirmed';order.due_date=date(2026,12,1);order.save()
        return order

    def test_finish_only_change_preserves_dimensions_discount_and_total(self):
        order=self.priced_order();before=order.total
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=order.total,
                         proposed_due_date=order.due_date,note='只改门板颜色')
        services.add_change_line(self.admin,change,dict(operation='update',order_line=order.lines.get(),specification='灰色'),change.version)
        change.refresh_from_db()
        self.assertEqual(change.proposed_total,before)
        services.transition(self.manager,change,'approve',change.version)
        order.refresh_from_db();line=order.lines.get()
        self.assertEqual(order.discount,Decimal('.95'))
        self.assertEqual(order.total,before)
        self.assertEqual(line.pricing_mode,'projected');self.assertEqual(line.billable_quantity,Decimal('8.32'))
        self.assertEqual(line.specification,'灰色');self.assertEqual(line.line_total,Decimal('10907.52'))

    def test_new_linear_and_non_discountable_fee_share_order_pricing(self):
        order=self.priced_order()
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=order.total,
                         proposed_due_date=order.due_date,note='新增台面及运费')
        for values in [dict(operation='add',name='台面',pricing_mode='linear',width_mm=2000,quantity=2,unit='m',unit_price=100),
                       dict(operation='add',name='运费',category='service',discountable=False,quantity=1,unit_price=200)]:
            services.add_change_line(self.admin,change,values,change.version);change.refresh_from_db()
        services.transition(self.manager,change,'approve',change.version)
        self.assertEqual(order.lines.get(name='台面').line_total,380)
        self.assertEqual(order.lines.get(name='运费').line_total,200)

    def test_remove_form_only_needs_target(self):
        order=self.priced_order()
        change=self.make(m.ChangeRequest,order=order,order_version=order.version,proposed_total=order.total,
                         proposed_due_date=order.due_date,note='移除')
        self.client.force_login(self.admin)
        response=self.client.post(f'/features/changes/{change.pk}/change-line/',
                                 {'version':change.version,'operation':'remove','order_line':order.lines.get().pk},follow=True)
        self.assertEqual(change.lines.count(),1)
        change.refresh_from_db();self.assertEqual(change.proposed_total,0)

    def test_zero_discount_gift_with_non_discounted_fee_can_confirm(self):
        quote=self.make(m.Quote,customer=self.customer,title='赠品方案',discount=0)
        for values in [dict(name='赠送柜体',quantity=1,unit_price=1000),dict(name='运费',quantity=1,unit_price=200,discountable=False)]:
            services.add_line(self.admin,quote,values,quote.version);quote.refresh_from_db()
        for action in ('submit','approve','accept'):
            services.transition(self.admin,quote,action,quote.version);quote.refresh_from_db()
        order=m.Order.objects.get(quote=quote);order.contract_number='GIFT';order.due_date=date(2026,12,1);order.save()
        services.transition(self.admin,order,'confirm',order.version)
        order.refresh_from_db();self.assertEqual(order.status,'confirmed');self.assertEqual(order.discount,0);self.assertEqual(order.total,200)

    def test_contextual_followup_list_filters_one_visible_customer(self):
        other=self.make(m.Customer,name='另一个客户',phone='1234')
        self.make(m.FollowUp,customer=self.customer,note='本客户沟通')
        self.make(m.FollowUp,customer=other,note='其他客户沟通')
        self.client.force_login(self.admin)
        response=self.client.get(f'/modules/followups/?customer={self.customer.pk}')
        self.assertEqual(response.context['page_obj'].paginator.count,1)
        self.assertContains(self.client.get(f'/modules/customers/{self.customer.pk}/'),'全部跟进记录')


class SecondReviewCollaborationTests(ERPTestCase):
    def setUp(self):
        super().setUp()
        self.factory=m.Organization.objects.create(name='授权工厂',kind='factory')
        self.other=m.Organization.objects.create(name='不可见外厂',kind='factory')

    def sale(self):
        order=self.order(status='confirmed',owner=self.sales)
        m.OrderLine.objects.create(order=order,name='衣柜',quantity=1,unit_price=1000,line_total=1000)
        return order

    def test_unconfigured_target_is_rejected_in_service_and_http(self):
        order=self.sale()
        with self.assertRaises(PermissionDenied):collaboration.send_handoff(self.sales,order,self.other,order.version)
        self.client.force_login(self.sales)
        self.assertNotContains(self.client.get(f'/modules/orders/{order.pk}/'),'不可见外厂')
        self.assertEqual(self.client.post(f'/features/orders/{order.pk}/handoff/',{'version':order.version,'target':self.other.pk}).status_code,404)
        self.assertFalse(m.Handoff.objects.exists())

    def test_source_only_sales_uses_route_without_factory_membership(self):
        self.assertTrue(hasattr(m,'CollaborationRoute'))
        m.CollaborationRoute.objects.create(source=self.org,target=self.factory)
        order=self.sale()
        handoff=collaboration.send_handoff(self.sales,order,self.factory,order.version)
        from erp.permissions import scoped
        self.assertFalse(m.Membership.objects.filter(user=self.sales,organization=self.factory).exists())
        self.assertFalse(scoped(self.sales,m.Order).filter(organization=self.factory).exists())
        self.assertEqual(handoff.target_organization,self.factory)

    def test_rejected_resend_rebuilds_payload_and_checks_both_versions(self):
        order=self.sale();handoff=collaboration.send_handoff(self.admin,order,self.factory,order.version)
        collaboration.accept_handoff(self.admin,handoff,handoff.version,reject=True,note='交期不合适')
        handoff.refresh_from_db();order.refresh_from_db()
        m.OrderLine.objects.filter(order=order).update(specification='重发新规格')
        sent=collaboration.send_handoff(self.admin,order,self.other,order.version,'改派',handoff_version=handoff.version)
        self.assertEqual(sent.pk,handoff.pk);self.assertEqual(sent.status,'sent')
        self.assertEqual(sent.payload['lines'][0]['specification'],'重发新规格')
        with self.assertRaises(ValidationError):collaboration.send_handoff(self.admin,order,self.factory,order.version,handoff_version=handoff.version)
        event=m.AuditEvent.objects.get(object_type='handoffs',object_id=handoff.pk,action='resend_handoff')
        self.assertEqual(event.before['target_organization'],str(self.factory.pk))

    def test_factory_reuses_internal_customer_and_crm_excludes_it(self):
        locals=[]
        for _ in range(2):
            order=self.sale();handoff=collaboration.send_handoff(self.admin,order,self.factory,order.version)
            locals.append(collaboration.accept_handoff(self.admin,handoff,handoff.version))
        self.assertEqual(locals[0].customer_id,locals[1].customer_id)
        self.assertTrue(locals[0].customer.is_internal)
        self.client.force_login(self.admin)
        response=self.client.get('/modules/customers/')
        self.assertNotContains(response,'生产协作')
        self.assertContains(self.client.get('/modules/customers/?include_internal=1'),'生产协作')
