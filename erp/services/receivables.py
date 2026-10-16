from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from erp import models as m
from erp.permissions import can,require_access
from .common import save_version,audit,number
from .pricing import rounded


@transaction.atomic
def create_plan(user,order,version,first_due,percentages):
    require_access(user,order,'view')
    if not can(user,'finance','create',order.organization_id) and not can(user,'changes','approve',order.organization_id):
        raise PermissionDenied('收款计划由财务或管理人员设置。')
    order=m.Order.objects.select_for_update().get(pk=order.pk)
    if order.version != int(version) or order.payment_plans.exists() or order.kind!='sale' or order.status=='cancelled':
        raise ValidationError('订单版本变化、已有计划或订单类型不支持。')
    ratios=[number(x,places=2,positive=True) for x in percentages]
    if len(ratios)!=3 or sum(ratios)!=100:
        raise ValidationError('三期比例之和必须为100%。')
    remaining=order.total
    for index,(name,gate,ratio) in enumerate(zip(['定金','生产款','尾款'],['start','ship','complete'],ratios),1):
        amount=rounded(order.total*ratio/100) if index<3 else remaining
        remaining-=amount
        due=first_due if index==1 else max(first_due,(order.due_date or first_due)-timedelta(days=7 if index==2 else 0))
        m.PaymentPlan.objects.create(organization=order.organization,owner=user,order=order,sequence=index,name=name,amount=amount,due_date=due,gate=gate)
    save_version(order,order.version)
    audit(user,order,'create_payment_plan')


def plan_rows(order):
    paid=order.payment_set.aggregate(s=Sum('amount'))['s'] or Decimal(0)
    result=[]
    for plan in order.payment_plans.all():
        allocated=min(paid,plan.amount); paid-=allocated
        balance=plan.amount-allocated
        result.append({'plan':plan,'paid':allocated,'balance':balance,
                       'overdue_days':max(0,(timezone.localdate()-plan.due_date).days) if balance else 0})
    return result


def check_gate(order,gate):
    plans=list(order.payment_plans.all())
    if not plans:return
    paid=order.payment_set.aggregate(s=Sum('amount'))['s'] or Decimal(0)
    cumulative=Decimal(0)
    for plan in plans:
        cumulative+=plan.amount
        if plan.gate==gate and paid<cumulative:
            raise ValidationError(f'履约收款前置条件未满足：{plan.name}前累计应收 {cumulative:.2f}，已收 {paid:.2f}。')
