from django.core.exceptions import ValidationError
from django.db import transaction
from erp import models as m
from .common import lock, snapshot, save_version, audit, system_update
from .stock import receive_purchase, issue_production
from erp.permissions import require_access

# Each action has a single legal source and target; prerequisites live below.
TRANSITIONS = {
    m.Customer: {'contact': ('new', 'contacted'), 'measure': ('contacted', 'measuring'),
                 'design': ('measuring', 'designing'), 'quote': ('designing', 'quoted'),
                 'win': ('quoted', 'won'), 'close': (('new', 'contacted', 'measuring', 'designing', 'quoted'), 'lost')},
    m.Measurement: {'assign': (('pending', 'failed'), 'assigned'), 'start': ('assigned', 'in_progress'),
                    'complete': ('in_progress', 'completed'), 'fail': ('in_progress', 'failed')},
    m.Design: {'submit': (('draft', 'rejected'), 'review'), 'approve': ('review', 'approved'), 'reject': ('review', 'rejected')},
    m.Quote: {'submit': (('draft', 'rejected'), 'review'), 'approve': ('review', 'approved'),
              'reject': ('review', 'rejected'), 'accept': ('approved', 'accepted')},
    m.Order: {'confirm': ('draft', 'confirmed'), 'cancel': (('draft', 'confirmed'), 'cancelled')},
    m.Breakdown: {'submit': ('draft', 'review'), 'release': ('review', 'released')},
    m.Production: {'plan': ('pending', 'planned'), 'issue': ('planned', 'issued'),
                   'start': ('issued', 'in_progress'), 'complete': ('in_progress', 'completed')},
    m.Purchase: {'submit': ('draft', 'review'), 'approve': ('review', 'approved'),
                 'reject': ('review', 'draft'), 'order': ('approved', 'ordered'), 'receive': (('ordered','partial'), 'received')},
    m.Delivery: {'assign': ('pending', 'assigned'), 'ship': ('assigned', 'shipped'), 'deliver': ('shipped', 'delivered')},
    m.Installation: {'assign': (('pending', 'failed'), 'assigned'), 'start': ('assigned', 'in_progress'),
                     'complete': ('in_progress', 'completed'), 'fail': ('in_progress', 'failed')},
    m.AfterSales: {'start': ('new', 'in_progress'), 'resolve': ('in_progress', 'resolved'), 'close': ('resolved', 'closed')},
    m.ChangeRequest: {'approve': ('pending', 'approved'), 'reject': ('pending', 'rejected'),'withdraw':('pending','withdrawn')},
    m.Handoff:{'accept':('sent','accepted'),'reject':('sent','rejected'),'withdraw':('sent','withdrawn')},
}
ACTION_LABELS = {'contact': '开始跟进', 'measure': '进入量尺', 'design': '进入设计', 'quote': '进入报价',
                 'win': '标记成交', 'close': '关闭', 'assign': '确认派工', 'start': '开始执行',
                 'complete': '完成验收', 'fail': '登记失败', 'submit': '提交审核', 'approve': '批准',
                 'reject': '驳回', 'accept': '接受并生成订单', 'confirm': '确认合同', 'cancel': '取消订单',
                 'release': '审核并释放工单', 'plan': '确认排程', 'issue': '领料', 'order': '采购下单',
                 'receive': '确认收货', 'ship': '发运', 'deliver': '签收', 'resolve': '确认解决','withdraw':'撤回申请'}


def available_actions(obj):
    labels = {
        m.Measurement: {'start': '开始量尺', 'complete': '提交量尺结果'},
        m.Production: {'start': '开始生产', 'complete': '确认完工'},
        m.Delivery: {'assign': '确认派车'},
        m.Installation: {'start': '开始安装', 'complete': '完成安装验收'},
        m.AfterSales: {'start': '开始处理'},
        m.Handoff:{'accept':'工厂接单','reject':'拒绝接单','withdraw':'撤回交接'},
    }.get(type(obj), {})
    return [(action, labels.get(action, ACTION_LABELS[action])) for action, (sources, target) in TRANSITIONS.get(type(obj), {}).items()
            if obj.status in (sources if isinstance(sources, tuple) else (sources,))]


def prerequisites(user, obj, action, note):
    from .receivables import check_gate
    if isinstance(obj,m.Production) and action=='start':
        order = obj.breakdown.order
        check_gate(order.source_handoff.order if order.source_handoff_id else order,'start')
    if isinstance(obj,m.Delivery) and action=='ship':check_gate(obj.order,'ship')
    if isinstance(obj,m.Installation) and action=='complete':check_gate(obj.order,'complete')
    if action in ('fail', 'reject', 'cancel', 'close', 'resolve','withdraw') and not note.strip():
        raise ValidationError('此操作必须填写原因或处理结论。')
    if action == 'complete' and isinstance(obj, (m.Measurement, m.Installation)) and not note.strip():
        raise ValidationError('请填写量尺结论或验收说明。')
    if isinstance(obj, m.Measurement) and action == 'complete' and not obj.area:
        raise ValidationError('请填写实测面积。')
    if isinstance(obj, (m.Quote, m.Purchase, m.Breakdown)) and action in ('submit', 'release') and not obj.lines.exists():
        raise ValidationError('至少添加一条明细后再提交。')
    if isinstance(obj, m.Order):
        if action=='cancel' and obj.source_handoff_id:
            raise ValidationError('已接单的工厂生产订单不能单边取消，请先协调源门店处理生产交接。')
        if action == 'confirm' and (not obj.contract_number.strip() or not obj.due_date or obj.total <= 0):
            raise ValidationError('确认前请补齐合同编号、交期及有效合同金额。')
        if action == 'cancel' and m.Breakdown.objects.filter(order=obj, status='released').exists():
            raise ValidationError('已释放生产的订单不能直接取消，请走售后及退料流程。')
        if action == 'cancel' and obj.payment_set.exists():
            raise ValidationError('已有收款的订单不能直接取消，请先由财务处理退款。')
        if action == 'cancel' and m.Handoff.objects.filter(order=obj,status__in=['sent','accepted']).exists():
            raise ValidationError('订单已有进行中的工厂交接，不能直接取消。')
    if isinstance(obj, m.Breakdown) and action in ('submit', 'release') and obj.order.status != 'confirmed':
        raise ValidationError('订单必须已确认且尚未开工。')
    if isinstance(obj,m.Breakdown) and m.Handoff.objects.filter(order=obj.order,status__in=['sent','accepted']).exists():
        raise ValidationError('源订单已交接，请在接单工厂的本地生产订单上拆单。')
    if isinstance(obj, m.Production) and action == 'plan' and (not obj.start_date or not obj.due_date or not obj.warehouse_id):
        raise ValidationError('排程需选择领料仓库、开工和完工日期。')
    if isinstance(obj, m.Production):
        expected_order_status = 'producing' if action == 'complete' else 'confirmed'
        if obj.breakdown.order.status != expected_order_status:
            raise ValidationError('订单状态已变化，不能继续当前生产操作。')
    if isinstance(obj, m.Delivery) and action == 'ship':
        if obj.order.status != 'ready':
            raise ValidationError('订单尚未生产完工，不能发运。')
        if not obj.vehicle.strip():
            raise ValidationError('请填写车牌或物流单号。')
    if isinstance(obj, m.Installation) and action == 'start' and obj.order.status != 'installing':
        raise ValidationError('订单必须已配送签收后才能开始安装。')
    if isinstance(obj,m.ChangeRequest) and action=='withdraw' and obj.requested_by_id!=user.pk:
        raise ValidationError('只有真实申请人可撤回待审批变更。')
    if isinstance(obj, m.ChangeRequest) and action == 'approve':
        last_editor = m.AuditEvent.objects.filter(object_type='changes', object_id=obj.pk, action='edit').first()
        if obj.requested_by_id == user.pk or (last_editor and last_editor.owner_id == user.pk):
            raise ValidationError('申请人不能审批自己的变更。')
        order = m.Order.objects.select_for_update().get(pk=obj.order_id)
        if order.source_handoff_id:
            raise ValidationError('工厂订单来自冻结的交接快照，不能单边修改产品与合同。')
        if order.version != obj.order_version:
            raise ValidationError('订单版本已变化，请重新申请变更。')
        if order.status not in ('confirmed', 'producing', 'ready', 'shipping', 'installing'):
            raise ValidationError('当前订单状态不允许变更。')
        received = sum((p.amount for p in order.payment_set.all()), 0)
        if obj.proposed_total < received:
            raise ValidationError('变更金额不能少于已收款。')
        if obj.proposed_total != order.total and order.payment_plans.exists() and not obj.revise_payment_plan:
            raise ValidationError('请在申请中勾选同步修订分期，系统将保留已收核销并重排未收金额。')
        if obj.proposed_total != order.total and order.payment_plans.exists():
            if sum((p.amount for p in order.payment_plans.all()),0)!=order.total:
                raise ValidationError('现有分期与原合同金额不一致，请先财务核对。')
        if m.Handoff.objects.filter(order=order,status__in=['sent','accepted','completed']).exists():
            raise ValidationError('订单已交接工厂，请先解决交接后再修改。')
        from .pricing import prepare_change
        prepare_change(obj,verify=True)
        if obj.lines.exists() or obj.proposed_discount is not None:
            if m.Breakdown.objects.filter(order=order).exists():
                raise ValidationError('订单已进入拆单，不能直接修改产品，请先处理生产变更。')


def effects(user, obj, action):
    if isinstance(obj, m.Quote) and action == 'accept':
        order = m.Order.objects.create(organization=obj.organization, owner=obj.owner, customer=obj.customer,
                               quote=obj, title=obj.title, total=obj.total,tax_rate=obj.tax_rate,discount=obj.discount,discount_confirmed=True)
        from .pricing import copy_quote_lines
        copy_quote_lines(obj,order)
        system_update(user, obj.customer, 'quote_accepted', status='won')
    elif isinstance(obj, m.Breakdown) and action == 'release':
        m.Production.objects.create(organization=obj.organization, owner=obj.owner, breakdown=obj)
    elif isinstance(obj, m.Production):
        if action == 'issue':
            issue_production(user, obj)
        elif action == 'start':
            system_update(user, obj.breakdown.order, 'production_started', status='producing')
            from .collaboration import return_progress
            return_progress(user,obj.breakdown.order,'producing')
        elif action == 'complete':
            system_update(user, obj.breakdown.order, 'production_completed', status='ready')
            from .collaboration import return_progress
            return_progress(user,obj.breakdown.order,'ready')
    elif isinstance(obj, m.Purchase) and action == 'receive':
        receive_purchase(user, obj)
    elif isinstance(obj, m.Delivery):
        if action == 'ship':
            system_update(user, obj.order, 'delivery_shipped', status='shipping')
        elif action == 'deliver':
            system_update(user, obj.order, 'delivery_received', status='installing')
    elif isinstance(obj, m.Installation) and action == 'complete':
        system_update(user, obj.order, 'installation_completed', status='completed')
    elif isinstance(obj, m.ChangeRequest) and action == 'approve':
        from .pricing import apply_change_lines
        from .receivables import reschedule_plan,revise_plan,plan_rows
        old_order=obj.order;allocations=plan_rows(old_order)
        apply_change_lines(user,obj)
        values={'total':obj.proposed_total,'due_date':obj.proposed_due_date}
        if obj.proposed_discount is not None:values.update(discount=obj.proposed_discount,discount_confirmed=True)
        updated=system_update(user, obj.order, 'approved_change', **values)
        if updated.total!=old_order.total:revise_plan(user,updated,allocations)
        reschedule_plan(user,updated)


@transaction.atomic
def transition(user, obj, action, version, note=''):
    if isinstance(obj,m.Handoff):
        from .collaboration import transition_handoff
        return transition_handoff(user,obj,action,version,note)
    require_access(user, obj, action)
    # Order cancellation and all fulfillment operations serialize on the same
    # parent row before child locks. PostgreSQL row locks protect cancel/release;
    # SQLite additionally uses compare-and-swap on every versioned write.
    fresh = type(obj).objects.get(pk=obj.pk)
    order_id = None
    if isinstance(fresh, m.Order):
        order_id = fresh.pk
    elif isinstance(fresh, (m.Breakdown, m.Delivery, m.Installation, m.AfterSales, m.ChangeRequest)):
        order_id = fresh.order_id
    elif isinstance(fresh, m.Production):
        order_id = fresh.breakdown.order_id
    if order_id:
        m.Order.objects.select_for_update().get(pk=order_id)
    obj = lock(user, obj, action, version)
    rule = TRANSITIONS.get(type(obj), {}).get(action)
    if not rule:
        raise ValidationError('不支持的业务操作。')
    sources, target = rule
    if obj.status not in (sources if isinstance(sources, tuple) else (sources,)):
        raise ValidationError('单据状态已变化或不允许此操作，请刷新后重试。')
    before = snapshot(obj)
    prerequisites(user, obj, action, note)
    expected = obj.version
    obj.status = target
    if note.strip():
        obj.note = note.strip()
    save_version(obj, expected)
    effects(user, obj, action)
    audit(user, obj, action, before)
    return obj
