from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.utils import timezone
from erp import models as m
from erp.permissions import can,scoped,require_access
from .common import lock, save_version, audit, snapshot, system_update

PRODUCTION_FIELDS = ('name','room','specification','quantity','unit','pricing_mode','category','width_mm','height_mm')


def route_enabled(user,handoff):
    return user.is_superuser or m.CollaborationRoute.objects.filter(
        source_id=handoff.organization_id,target_id=handoff.target_organization_id,enabled=True).exists()


def allowed_factories(user,source):
    if not can(user,'orders','edit',source.pk):return m.Organization.objects.none()
    factories=m.Organization.objects.filter(kind='factory').exclude(pk=source.pk)
    if not user.is_superuser:
        factories=factories.filter(incoming_routes__source=source,incoming_routes__enabled=True)
    return factories


@transaction.atomic
def send_handoff(user,order,target,version,note='',handoff_version=None):
    order = lock(user,order,'edit',version)
    if not allowed_factories(user,order.organization).filter(pk=target.pk).exists():
        raise PermissionDenied('此门店未被授权向该工厂交接。')
    if order.kind != 'sale' or order.status != 'confirmed' or not order.lines.exists():
        raise ValidationError('仅已确认且具有产品明细的销售订单可以交接。')
    existing=m.Handoff.objects.select_for_update().filter(order=order).first()
    if m.Breakdown.objects.filter(order=order).exists():
        raise ValidationError('订单已有拆单，不能交接。')
    if existing:
        try:expected=int(handoff_version)
        except (TypeError,ValueError):raise ValidationError('重发需有效的交接版本，请刷新。')
        if existing.status not in ('rejected','withdrawn') or existing.version!=expected:
            raise ValidationError('只有已拒单或已撤回的交接可重发，请刷新。')
    if existing and existing.target_organization_id!=target.pk:
        files=list(m.Attachment.objects.select_for_update().filter(object_type='handoffs',object_id=existing.pk,
                   organization=order.organization,deleted_at__isnull=True,archived_at__isnull=True))
        if files:
            before={'target':existing.target_organization_id,'attachments':[snapshot(item) for item in files]}
            for item in files:
                item.archived_at=timezone.now();item.archive_reason='reassigned';save_version(item,item.version)
            audit(user,existing,'archive_handoff_evidence',before,
                  after={'target':target.pk,'attachments':[snapshot(item) for item in files],
                         'policy':'保留旧工厂证据，仅源门店可查阅，不向新工厂共享。'})
    payload = {'title':order.title,'source_code':order.code,'source_version':order.version,
               'due_date':str(order.due_date or ''),
               'lines':[{field:str(getattr(line,field)) for field in PRODUCTION_FIELDS} for line in order.lines.filter(category='product')]}
    if not payload['lines']:
        raise ValidationError('至少需要一个实物产品才能交接生产。')
    before=snapshot(existing) if existing else None
    if existing:
        handoff=existing
        handoff.target_organization=target;handoff.payload=payload;handoff.note=note;handoff.status='sent';handoff.owner=user
        save_version(handoff,handoff.version)
    else:
        handoff = m.Handoff(organization=order.organization,owner=user,order=order,target_organization=target,payload=payload,note=note)
        handoff.full_clean(exclude=['code']);handoff.save()
    save_version(order,order.version)
    audit(user,handoff,'resend_handoff' if existing else 'send_handoff',before)
    audit(user,order,'send_handoff',after={'handoff':handoff.code,'target':target.name})
    return handoff


@transaction.atomic
def accept_handoff(user,handoff,version,reject=False,note=''):
    # Both target and source operations lock the source order before handoff.
    source = m.Order.objects.select_for_update().get(pk=handoff.order_id)
    handoff = m.Handoff.objects.select_for_update().get(pk=handoff.pk)
    if not can(user,'handoffs','accept',handoff.target_organization_id):
        raise PermissionDenied('只有接单工厂的授权人员可处理交接。')
    if not reject and not route_enabled(user,handoff):
        raise PermissionDenied('源门店与本工厂的协作授权未启用，请联系管理员。')
    if handoff.status != 'sent' or handoff.version != int(version):
        raise ValidationError('交接状态或版本已变化，请刷新。')
    if reject:
        if not note.strip():
            raise ValidationError('拒单需要原因。')
        before=snapshot(handoff)
        handoff.status, handoff.note = 'rejected', note
        save_version(handoff,handoff.version)
        audit(user,handoff,'reject_handoff',before)
        return handoff
    if source.status != 'confirmed':
        raise ValidationError('源订单已变化，不能接单。')
    before = snapshot(handoff)
    target = handoff.target_organization
    customer,_ = m.Customer.objects.get_or_create(organization=target,internal_source=handoff.organization,
                   defaults={'owner':user,'name':f'{handoff.organization.name} · 生产协作','phone':'内部协作','source':'门厂交接','is_internal':True})
    order = m.Order.objects.create(organization=target,owner=user,customer=customer,title=handoff.payload['title'],
                                   kind='manufacturing',source_handoff=handoff,status='confirmed',discount_confirmed=True,
                                   due_date=source.due_date,contract_number=f'JOB-{handoff.code}')
    for line in handoff.payload['lines']:
        m.OrderLine.objects.create(order=order,**line,line_total=0,unit_price=0,unit_cost=0)
    handoff.status = 'accepted'
    save_version(handoff,handoff.version)
    audit(user,handoff,'accept_handoff',before)
    audit(user,order,'factory_order_created')
    return order


def can_withdraw_handoff(user,handoff):
    return can(user,'handoffs','withdraw',handoff.organization_id) and scoped(user,m.Order,action='edit').filter(pk=handoff.order_id).exists()


def handoff_actions(user,handoff):
    from .workflows import available_actions
    result=[]
    for action,label in available_actions(handoff):
        if action=='withdraw':
            allowed=can_withdraw_handoff(user,handoff)
        else:
            allowed=can(user,'handoffs','accept',handoff.target_organization_id)
            if action=='accept':allowed=allowed and route_enabled(user,handoff)
        if allowed:result.append((action,label))
    return result


@transaction.atomic
def withdraw_handoff(user,handoff,version,note):
    source=m.Order.objects.select_for_update().get(pk=handoff.order_id)
    require_access(user,source,'edit')
    if not can(user,'handoffs','withdraw',source.organization_id):
        raise PermissionDenied('当前角色没有源门店交接撤回权限。')
    handoff=m.Handoff.objects.select_for_update().get(pk=handoff.pk)
    try:expected=int(version)
    except (ValueError,TypeError):raise ValidationError('缺少有效交接版本。')
    if handoff.status!='sent' or handoff.version!=expected:
        raise ValidationError('仅当前待接单交接可撤回；已接单不能单边撤回。')
    if not note.strip():raise ValidationError('请填写撤回原因。')
    before=snapshot(handoff);handoff.status='withdrawn';handoff.note=note.strip()
    save_version(handoff,expected);save_version(source,source.version)
    audit(user,handoff,'withdraw_handoff',before)
    audit(user,source,'handoff_withdrawn',after={'handoff':handoff.code,'reason':note.strip()})
    return handoff


def transition_handoff(user,handoff,action,version,note=''):
    if action=='withdraw':return withdraw_handoff(user,handoff,version,note)
    if action in ('accept','reject'):return accept_handoff(user,handoff,version,reject=action=='reject',note=note)
    raise ValidationError('不支持的交接操作。')


@transaction.atomic
def return_progress(user,factory_order,status):
    if status not in ('producing','ready'):
        raise ValidationError('仅支持开工或完工回传。')
    if not factory_order.source_handoff_id:
        return
    source_id=m.Handoff.objects.values_list('order_id',flat=True).get(pk=factory_order.source_handoff_id)
    source = m.Order.objects.select_for_update().get(pk=source_id)
    handoff = m.Handoff.objects.select_for_update().get(pk=factory_order.source_handoff_id)
    expected = 'confirmed' if status=='producing' else 'producing'
    if source.status != expected:
        raise ValidationError('门店订单状态已变化，不能回传生产进度。')
    system_update(user,source,'factory_progress',status=status)
    if status=='ready':
        handoff.status='completed'
        save_version(handoff,handoff.version)
        audit(user,handoff,'factory_completed')
