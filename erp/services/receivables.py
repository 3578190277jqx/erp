from datetime import timedelta
from decimal import Decimal
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from erp import models as m
from erp.permissions import can,require_access
from .common import save_version,audit,number,snapshot
from .pricing import rounded


@transaction.atomic
def create_plan(user,order,version,first_due,percentages):
    require_access(user,order,'view')
    if not can(user,'finance','create',order.organization_id) and not can(user,'changes','approve',order.organization_id):
        raise PermissionDenied('收款计划由财务或管理人员设置。')
    order=m.Order.objects.select_for_update().get(pk=order.pk)
    if order.version != int(version) or order.payment_plans.exists() or order.kind!='sale' or order.status!='confirmed' or order.total<=0:
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
    payments=list(order.payment_set.values('plan_id').annotate(s=Sum('amount')))
    explicit={item['plan_id']:item['s'] for item in payments if item['plan_id'] is not None}
    paid=sum((item['s'] for item in payments if item['plan_id'] is None),Decimal(0))
    result=[]
    for plan in order.payment_plans.all():
        assigned=explicit.get(plan.pk,Decimal(0))
        allocated=min(paid,max(Decimal(0),plan.amount-assigned));paid-=allocated
        allocated+=assigned
        balance=plan.amount-allocated
        result.append({'plan':plan,'paid':allocated,'balance':balance,
                       'overdue_days':max(0,(timezone.localdate()-plan.due_date).days) if balance else 0})
    return result


def check_gate(order,gate):
    plans=list(order.payment_plans.all())
    if not plans:
        if order.organization.require_payment_plan:
            raise ValidationError('本组织要求分期计划，请先由财务启用后再履约。')
        return
    if sum((plan.amount for plan in plans),Decimal(0))!=order.total:
        raise ValidationError('分期合计与合同金额不一致，请由财务核对后继续。')
    unsettled=Decimal(0)
    for row in plan_rows(order):
        unsettled+=row['balance']
        if row['plan'].gate==gate and unsettled>0:
            raise ValidationError(f'履约收款前置条件未满足：{row["plan"].name}及之前期次仍未收 {unsettled:.2f}。')


def reschedule_plan(user,order):
    plans=list(order.payment_plans.select_for_update())
    if not plans or not order.due_date:return
    first_due=plans[0].due_date
    for plan in plans[1:]:
        due=max(first_due,order.due_date-timedelta(days=7 if plan.sequence==2 else 0))
        if plan.due_date!=due:
            before=snapshot(plan);plan.due_date=due;save_version(plan,plan.version)
            audit(user,plan,'reschedule_payment_plan',before)


def revised_amounts(total,previous_rows):
    """Pure preview shared with approval; preserve every historical allocation."""
    if not previous_rows:return []
    from decimal import ROUND_DOWN
    paid=[row['paid'] for row in previous_rows]
    if any(row['balance']<0 for row in previous_rows):raise ValidationError('分期已收数据异常，请财务核对。')
    remaining=total-sum(paid,Decimal(0))
    if remaining<0:raise ValidationError('新合同金额不能低于已收。')
    weights=[row['balance'] for row in previous_rows];weight=sum(weights,Decimal(0))
    cents=int(remaining*100)
    shares=[Decimal(cents)*value/weight for value in weights] if weight else [Decimal(0)]*len(previous_rows)
    allocated=[int(value.to_integral_value(rounding=ROUND_DOWN)) for value in shares]
    if weight:
        ranked=sorted(range(len(previous_rows)),key=lambda i:(-(shares[i]-allocated[i]),i))
        for index in ranked[:cents-sum(allocated)]:allocated[index]+=1
    else:allocated[-1]=cents
    return [paid[i]+Decimal(value)/100 for i,value in enumerate(allocated)]


def revise_plan(user,order,previous_rows):
    """Preserve allocations; spread only new outstanding money, in cents."""
    if not previous_rows:return
    plans=list(order.payment_plans.select_for_update())
    if [p.pk for p in plans]!=[row['plan'].pk for row in previous_rows]:
        raise ValidationError('分期记录已变化，请重新核对。')
    amounts=revised_amounts(order.total,previous_rows)
    before={'plans':[snapshot(plan) for plan in plans]}
    for i,plan in enumerate(plans):
        old=snapshot(plan);plan.amount=amounts[i]
        if plan.amount!=Decimal(old['amount']):
            save_version(plan,plan.version);audit(user,plan,'revise_payment_plan',old)
    audit(user,order,'revise_payment_plan',before,after={'plans':[snapshot(plan) for plan in plans],
          'policy':'已收核销保持，按原未收比例分配；原全部结清时新增未收归尾款。'})
