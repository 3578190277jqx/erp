from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from erp import models as m
from erp.permissions import can
from .common import lock, save_version, audit, snapshot, system_update

PRODUCTION_FIELDS = ('name','room','specification','quantity','unit','pricing_mode','category','width_mm','height_mm')


@transaction.atomic
def send_handoff(user,order,target,version,note=''):
    order = lock(user,order,'edit',version)
    if order.kind != 'sale' or order.status != 'confirmed' or not order.lines.exists():
        raise ValidationError('仅已确认且具有产品明细的销售订单可以交接。')
    if m.Breakdown.objects.filter(order=order).exists() or m.Handoff.objects.filter(order=order).exists():
        raise ValidationError('订单已有拆单或交接记录，不能重复发送。')
    payload = {'title':order.title,'source_code':order.code,'source_version':order.version,
               'due_date':str(order.due_date or ''),
               'lines':[{field:str(getattr(line,field)) for field in PRODUCTION_FIELDS} for line in order.lines.filter(category='product')]}
    if not payload['lines']:
        raise ValidationError('至少需要一个实物产品才能交接生产。')
    handoff = m.Handoff(organization=order.organization,owner=user,order=order,target_organization=target,payload=payload,note=note)
    handoff.full_clean(exclude=['code'])
    handoff.save()
    save_version(order,order.version)
    audit(user,handoff,'send_handoff')
    audit(user,order,'send_handoff',after={'handoff':handoff.code,'target':target.name})
    return handoff


@transaction.atomic
def accept_handoff(user,handoff,version,reject=False,note=''):
    # Both target and source operations lock the source order before handoff.
    source = m.Order.objects.select_for_update().get(pk=handoff.order_id)
    handoff = m.Handoff.objects.select_for_update().get(pk=handoff.pk)
    if not can(user,'handoffs','accept',handoff.target_organization_id):
        raise PermissionDenied('只有接单工厂的授权人员可处理交接。')
    if handoff.status != 'sent' or handoff.version != int(version):
        raise ValidationError('交接状态或版本已变化，请刷新。')
    if reject:
        if not note.strip():
            raise ValidationError('拒单需要原因。')
        handoff.status, handoff.note = 'rejected', note
        save_version(handoff,handoff.version)
        audit(user,handoff,'reject_handoff')
        return handoff
    if source.status != 'confirmed':
        raise ValidationError('源订单已变化，不能接单。')
    before = snapshot(handoff)
    target = handoff.target_organization
    customer = m.Customer.objects.create(organization=target,owner=user,name=f'{handoff.organization.name} · 生产协作',phone='内部协作',source='门厂交接')
    order = m.Order.objects.create(organization=target,owner=user,customer=customer,title=handoff.payload['title'],
                                   kind='manufacturing',source_handoff=handoff,status='confirmed',
                                   due_date=source.due_date,contract_number=f'JOB-{handoff.code}')
    for line in handoff.payload['lines']:
        m.OrderLine.objects.create(order=order,**line,line_total=0,unit_price=0,unit_cost=0)
    handoff.status = 'accepted'
    save_version(handoff,handoff.version)
    audit(user,handoff,'accept_handoff',before)
    audit(user,order,'factory_order_created')
    return order


def return_progress(user,factory_order,status):
    if not factory_order.source_handoff_id:
        return
    handoff = m.Handoff.objects.select_for_update().get(pk=factory_order.source_handoff_id)
    source = m.Order.objects.select_for_update().get(pk=handoff.order_id)
    expected = 'confirmed' if status=='producing' else 'producing'
    if source.status != expected:
        raise ValidationError('门店订单状态已变化，不能回传生产进度。')
    system_update(user,source,'factory_progress',status=status)
    if status=='ready':
        handoff.status='completed'
        save_version(handoff,handoff.version)
        audit(user,handoff,'factory_completed')
