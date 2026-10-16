"""Explicit commercial amounts. No browser-calculated totals are trusted."""
from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db import transaction
from erp import models as m
from .common import lock, audit, snapshot, save_version

CENT = Decimal('.01')
SNAPSHOT_FIELDS = ('name','room','specification','quantity','unit','unit_price','pricing_mode','category',
                   'width_mm','height_mm','unit_cost','discountable')


def rounded(value):
    return Decimal(value).quantize(CENT,rounding=ROUND_HALF_UP)


def line_net(line,discount):
    base=rounded(line.billable_quantity*line.unit_price)
    return rounded(base*(discount if line.discountable else 1))


def quote_amounts(quote):
    net, cost = Decimal(0), Decimal(0)
    for line in quote.lines.all():
        net += line_net(line,quote.discount)
        cost += rounded(line.billable_quantity * line.unit_cost)
    tax = rounded(net * quote.tax_rate)
    return {'net':net,'tax':tax,'total':net+tax,'cost':cost,'margin':net-cost}


def copy_quote_lines(quote, order):
    for line in quote.lines.all():
        values = {name:getattr(line,name) for name in SNAPSHOT_FIELDS}
        m.OrderLine.objects.create(order=order,source_quote_line_id=line.pk,
            line_total=line_net(line,order.discount),**values)


def proposed_lines(change):
    lines = {line.pk:{field:getattr(line,field) for field in SNAPSHOT_FIELDS} | {'line_total':line.line_total}
             for line in change.order.lines.all()}
    for line in change.lines.all():
        if line.operation in ('update','remove') and line.order_line_id not in lines:
            raise ValidationError('原订单产品不存在或被重复修改。')
        if line.operation in ('update','remove'):
            del lines[line.order_line_id]
        if line.operation != 'remove':
            lines[f'new-{line.pk}'] = {key:getattr(line,key) for key in SNAPSHOT_FIELDS}
            lines[f'new-{line.pk}']['line_total'] = line_net(line,change.order.discount)
    return lines


@transaction.atomic
def add_change_line(user, change, values, version):
    # Lock the order first, consistent with approval and fulfillment.
    m.Order.objects.select_for_update().get(pk=change.order_id)
    change = lock(user,change,'edit',version)
    if change.status != 'pending':
        raise ValidationError('已审批的变更不能修改。')
    allowed = {'operation','order_line',*SNAPSHOT_FIELDS}
    if set(values)-allowed:
        raise ValidationError('变更明细字段无效。')
    target = values.get('order_line')
    values=dict(values)
    if target:
        target=m.OrderLine.objects.get(pk=target.pk)
        values['order_line']=target
    if target and target.order_id != change.order_id:
        raise ValidationError('原产品不属于当前订单。')
    if values.get('operation') in ('remove','update') and not target:
        raise ValidationError('请选择需要变更的原产品。')
    if values.get('operation')=='update' and target:
        values={field:getattr(target,field) for field in SNAPSHOT_FIELDS}|values
    if values.get('operation') != 'remove' and not values.get('name','').strip():
        raise ValidationError('增项或修改需要产品名称。')
    before = snapshot(change)
    line = m.ChangeLine(change=change,**values)
    line.full_clean()
    line.save()
    proposed = proposed_lines(change)
    net = sum((v['line_total'] for v in proposed.values()),Decimal(0))
    change.proposed_total = net + rounded(net*change.order.tax_rate)
    save_version(change,version)
    audit(user,change,'add_change_line',before)
    return change


def apply_change_lines(user,change):
    # Historical rows are retained via before/after audit snapshots; protected BOM
    # anchors prevent dropping products that already drove manufacturing.
    before = {'lines':[snapshot(line) for line in change.order.lines.all()]}
    for line in change.lines.all():
        if line.operation == 'remove':
            line.order_line.delete()
        else:
            values = {k:getattr(line,k) for k in SNAPSHOT_FIELDS}
            values['line_total'] = line_net(line,change.order.discount)
            if line.operation == 'update':
                m.OrderLine.objects.filter(pk=line.order_line_id).update(**values)
            else:
                m.OrderLine.objects.create(order=change.order,**values)
    audit(user,change.order,'change_products',before=before,
          after={'change':change.code,'lines':[snapshot(line) for line in change.order.lines.all()]})
