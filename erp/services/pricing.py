"""Explicit commercial amounts. No browser-calculated totals are trusted."""
from decimal import Decimal, ROUND_HALF_UP
from dataclasses import dataclass
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


def change_discount(change):
    return change.proposed_discount if change.proposed_discount is not None else change.order.discount


def validate_pricing_basis(change):
    if not change.order.discount_confirmed and change.proposed_discount is None:
        raise ValidationError('历史合同折扣口径未知，请在变更申请中明确拟采用折扣并经审批核对。')


def _preview_key(change):
    order=change.order
    return (change.pk,change.version,order.pk,order.version,change.proposed_total,change.proposed_discount,
            change.restore_product_snapshot,order.total,order.discount,order.tax_rate,order.discount_confirmed)


@dataclass(frozen=True)
class ChangePricingSnapshot:
    key: tuple
    source_lines: tuple
    change_lines: tuple
    _proposed: dict | None
    _error: ValidationError | None

    def check(self,change):
        if self.key!=_preview_key(change):raise ValidationError('计价快照与当前申请不一致，请刷新重新核对。')

    def proposed(self):
        if self._error is not None:raise self._error
        return self._proposed


def snapshot_change(change):
    """Request-local calculation only; write callers rebuild under their locks."""
    source=tuple(change.order.lines.all())
    changes=tuple(change.lines.select_related('order_line').all()) if change.pk else ()
    result=error=None
    try:result=proposed_lines(change,source_lines=source,change_lines=changes)
    except ValidationError as exc:error=exc
    return ChangePricingSnapshot(_preview_key(change),source,changes,result,error)


def change_total(change,*,pricing_snapshot=None):
    # A discount declaration without any product evidence cannot reconstruct a
    # historical contract amount. Preserve that header until a full restore.
    review=pricing_snapshot or snapshot_change(change);review.check(change)
    if not review.source_lines and not review.change_lines:
        return change.order.total
    net=sum((line['line_total'] for line in review.proposed().values()),Decimal(0))
    return net+rounded(net*change.order.tax_rate)


UNSET=object()


def prepare_change(change, *, submitted_total=UNSET, verify=False, persist_lines=False,pricing_snapshot=None):
    """Prepare a proposal under its parent lock, or verify it without mutation.

    Only form preparation passes submitted_total: None means carry the current
    contract. Stored zero is never inferred to be missing at approval time.
    """
    order=change.order
    review=pricing_snapshot or snapshot_change(change);review.check(change)
    has_changes=bool(review.change_lines)
    has_products=bool(review.source_lines)
    derived=has_changes or change.proposed_discount is not None
    if derived:
        total=change_total(change,pricing_snapshot=review)
    elif submitted_total is not UNSET:
        total=order.total if submitted_total is None else submitted_total
    else:
        total=change.proposed_total
    total=change._meta.get_field('proposed_total').clean(total,change)
    if derived or total!=order.total:validate_pricing_basis(change)
    if has_changes and not has_products and order.total>0 and not change.restore_product_snapshot:
        raise ValidationError('原合同缺少产品快照，请明确完整补录全部产品，不能只填写新增费用。')
    if verify and has_products and not derived and total!=order.total:
        raise ValidationError('有产品明细的订单必须通过增减项计算变更金额。')
    if verify:
        if change.proposed_total!=total:
            raise ValidationError('变更总额与产品增减项不一致，请重新核对。')
        return total
    change.proposed_total=total
    if persist_lines and has_changes:
        for line in review.change_lines:
            line.net_amount=line_net(line,change_discount(change)) if line.operation!='remove' else Decimal(0)
            line.full_clean();line.save(update_fields=['net_amount'])
    return total


def proposed_lines(change,*,source_lines=None,change_lines=None):
    source_lines=tuple(change.order.lines.all()) if source_lines is None else source_lines
    change_lines=(change.lines.all() if change.pk else ()) if change_lines is None else change_lines
    lines = {line.pk:{field:getattr(line,field) for field in SNAPSHOT_FIELDS} | {'line_total':line.line_total}
             for line in source_lines}
    if change.proposed_discount is not None:
        for current in source_lines:lines[current.pk]['line_total']=line_net(current,change.proposed_discount)
    source_ids=set(lines)
    for line in change_lines:
        if line.operation in ('update','remove') and line.order_line_id not in lines:
            if line.order_line_id not in source_ids:
                raise ValidationError('原产品已被删除，请移除对应变更行。',code='missing_source_line')
            raise ValidationError('同一原产品被重复修改，请移除重复变更行。',code='duplicate_source_line')
        if line.operation in ('update','remove'):
            del lines[line.order_line_id]
        if line.operation != 'remove':
            lines[f'new-{line.pk}'] = {key:getattr(line,key) for key in SNAPSHOT_FIELDS}
            lines[f'new-{line.pk}']['line_total'] = line_net(line,change_discount(change))
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
    line.net_amount=line_net(line,change_discount(change)) if line.operation!='remove' else Decimal(0)
    line._meta.get_field('net_amount').clean(line.net_amount,line)
    line.save()
    prepare_change(change)
    save_version(change,version)
    audit(user,change,'add_change_line',before)
    return change


@transaction.atomic
def remove_change_line(user,change,line_id,version):
    parent=m.Order.objects.select_for_update().get(pk=change.order_id)
    change=lock(user,change,'edit',version);change.order=parent
    if change.status!='pending':raise ValidationError('仅待审批申请可移除变更行。')
    try:line_id=int(line_id)
    except (ValueError,TypeError):raise ValidationError('请选择有效的申请行。')
    line=change.lines.select_for_update().filter(pk=line_id).first()
    if not line:raise ValidationError('此行不属于当前申请或已移除。')
    before={'change':snapshot(change),'line':snapshot(line)}
    line.delete();recalculation_error=''
    try:prepare_change(change,submitted_total=None)
    except ValidationError as exc:
        # Removing one stale line must not roll back just because another stale
        # target remains. Keep the previous proposal explicitly unreviewed;
        # approval still verifies every remaining line and the order version.
        if getattr(exc,'code',None) not in ('missing_source_line','duplicate_source_line'):raise
        recalculation_error='；'.join(exc.messages)
    save_version(change,change.version)
    audit(user,change,'remove_change_line',before,after={'change':snapshot(change),'recalculation_error':recalculation_error,
          'recalculated_against_order_version':None if recalculation_error else parent.version,
          'attempted_order_version':parent.version})
    return change


def apply_change_lines(user,change):
    # Historical rows are retained via before/after audit snapshots; protected BOM
    # anchors prevent dropping products that already drove manufacturing.
    before = {'discount':str(change.order.discount),'lines':[snapshot(line) for line in change.order.lines.all()]}
    for line in change.lines.all():
        if line.operation == 'remove':
            line.order_line.delete()
        else:
            values = {k:getattr(line,k) for k in SNAPSHOT_FIELDS}
            values['line_total'] = line_net(line,change_discount(change))
            line.net_amount=values['line_total'];line.save(update_fields=['net_amount'])
            if line.operation == 'update':
                m.OrderLine.objects.filter(pk=line.order_line_id).update(**values)
            else:
                m.OrderLine.objects.create(order=change.order,**values)
    if change.proposed_discount is not None:
        for item in change.order.lines.all():
            item.line_total=line_net(item,change.proposed_discount)
            item.full_clean();item.save(update_fields=['line_total'])
    audit(user,change.order,'change_products',before=before,
          after={'change':change.code,'discount':str(change_discount(change)),'lines':[snapshot(line) for line in change.order.lines.all()]})
