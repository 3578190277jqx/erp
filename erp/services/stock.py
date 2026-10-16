import uuid
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from erp.models import Stock, StockMovement
from erp.permissions import require_access
from .common import number, audit


def move(user, warehouse, material, delta, reference, reason):
    if warehouse.organization_id != material.organization_id:
        raise ValidationError('仓库与物料必须属于同一组织。')
    stock, _ = Stock.objects.get_or_create(warehouse=warehouse, material=material,
                                         defaults={'organization': warehouse.organization, 'owner': user})
    stock = Stock.objects.select_for_update().get(pk=stock.pk)
    if not Stock.objects.filter(pk=stock.pk, quantity__gte=F('reserved')-delta).update(quantity=F('quantity') + delta, version=F('version') + 1):
        raise ValidationError(f'{material.name} 库存不足，需要 {-delta} {material.unit}，现存 {stock.quantity}。')
    stock.refresh_from_db()
    movement = StockMovement(organization=warehouse.organization, owner=user, warehouse=warehouse,
                            material=material, delta=delta, balance=stock.quantity, reference=reference, note=reason)
    movement.full_clean(exclude=['code'])
    movement.save()
    return movement


@transaction.atomic
def adjust_stock(user, warehouse, material, quantity, reason):
    require_access(user, warehouse, 'adjust')
    # Related material visibility is required, even when a user knows its ID.
    require_access(user, material, 'view')
    delta = number(quantity, places=3)
    if delta == 0 or not reason.strip():
        raise ValidationError('调整数量不能为零，且必须填写调整原因。')
    movement = move(user, warehouse, material, delta, f'ADJ-{uuid.uuid4().hex}', reason.strip())
    audit(user, movement, 'stock_adjustment')
    return movement


def receive_purchase(user, purchase):
    lines = list(purchase.lines.select_related('material').all())
    if not lines:
        raise ValidationError('空采购单不能收货。')
    from .materials import post_receipt
    quantities={line.pk:line.quantity-line.received_qty for line in lines if line.quantity>line.received_qty}
    post_receipt(user,purchase,quantities,f'AUTO-{purchase.code}-{purchase.version}')


def issue_production(user, production):
    if not production.warehouse_id:
        raise ValidationError('请先选择领料仓库。')
    lines = list(production.breakdown.lines.select_related('material', 'breakdown').order_by('material_id'))
    if not lines:
        raise ValidationError('BOM 为空，不能领料。')
    for line in lines:
        from erp.models import StockReservation
        reservation=StockReservation.objects.select_for_update().filter(production=production,stock__warehouse=production.warehouse,
                                                                         stock__material=line.material,status='active').first()
        if reservation:
            if reservation.quantity != line.required:
                raise ValidationError('预留与 BOM 需求不一致，请释放后重新预留。')
            Stock.objects.filter(pk=reservation.stock_id).update(reserved=F('reserved')-reservation.quantity)
            reservation.status='consumed'; reservation.save()
        move(user, production.warehouse, line.material, -line.required,
             f'ISSUE-{production.pk}', f'生产领料 {production.code}')
