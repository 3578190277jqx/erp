from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from erp import models as m
from erp.permissions import require_access
from .common import lock,save_version,audit,snapshot,number


@transaction.atomic
def reserve_production(user,production,version,release=False):
    m.Order.objects.select_for_update().get(pk=production.breakdown.order_id)
    production=lock(user,production,'reserve',version)
    if production.status != 'planned' or not production.warehouse_id:
        raise ValidationError('只有已排程且选择仓库的工单可预留或释放。')
    active=production.reservations.filter(status='active').select_related('stock').order_by('stock_id')
    if release:
        for reservation in active:
            m.Stock.objects.filter(pk=reservation.stock_id).update(reserved=F('reserved')-reservation.quantity,version=F('version')+1)
            reservation.status='released'; reservation.save()
    else:
        if active.exists():
            raise ValidationError('工单已预留，不能重复预留。')
        lines=list(production.breakdown.lines.select_related('material','breakdown').order_by('material_id'))
        if not lines:
            raise ValidationError('BOM 没有物料。')
        for line in lines:
            stock=m.Stock.objects.select_for_update().filter(warehouse=production.warehouse,material=line.material).first()
            if not stock or not m.Stock.objects.filter(pk=stock.pk,quantity__gte=F('reserved')+line.required).update(reserved=F('reserved')+line.required,version=F('version')+1):
                raise ValidationError(f'{line.material.name} 可用库存不足，预留已全部回滚。')
            m.StockReservation.objects.create(organization=production.organization,owner=user,production=production,stock=stock,quantity=line.required)
    save_version(production,production.version)
    audit(user,production,'release_reservation' if release else 'reserve_stock')
    return production


def post_receipt(user,purchase,quantities,reference):
    from .stock import move
    if not reference.strip() or len(reference)>80 or m.PurchaseReceipt.objects.filter(reference=reference).exists():
        raise ValidationError('到货凭证号不能为空、超长或重复。')
    if not quantities:
        raise ValidationError('至少填写一条实收数量。')
    lines={line.pk:line for line in purchase.lines.select_for_update().select_related('material').order_by('material_id','id')}
    receipt=m.PurchaseReceipt.objects.create(organization=purchase.organization,owner=user,purchase=purchase,reference=reference)
    for raw_pk,value in quantities.items():
        try: line=lines[int(raw_pk)]
        except (KeyError,ValueError,TypeError): raise ValidationError('采购明细不属于当前单据。')
        quantity=number(value,places=3,positive=True)
        if quantity > line.quantity-line.received_qty:
            raise ValidationError(f'{line.material.name} 本次实收超过剩余未收数量。')
        m.ReceiptLine.objects.create(receipt=receipt,purchase_line=line,quantity=quantity)
        move(user,purchase.warehouse,line.material,quantity,f'RECEIPT-{receipt.pk}-LINE-{line.pk}',f'分批收货 {reference}')
        line.received_qty += quantity; line.save(update_fields=['received_qty'])
    audit(user,receipt,'partial_receipt')
    return receipt


@transaction.atomic
def receive_partial(user,purchase,quantities,reference,version):
    purchase=lock(user,purchase,'receive',version)
    if purchase.status not in ('ordered','partial'):
        raise ValidationError('只有已下单或部分到货的采购可以收货。')
    before=snapshot(purchase)
    receipt=post_receipt(user,purchase,quantities,reference)
    purchase.status='partial' if purchase.lines.filter(received_qty__lt=F('quantity')).exists() else 'received'
    save_version(purchase,version)
    audit(user,purchase,'partial_receipt',before)
    return receipt
