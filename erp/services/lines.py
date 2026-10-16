from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from erp.models import Quote, QuoteLine, Purchase, PurchaseLine, Breakdown, BOMLine
from erp.permissions import require_access
from .common import lock, snapshot, save_version, audit, number

LINE_MODELS = {Quote: (QuoteLine, 'quote'), Purchase: (PurchaseLine, 'purchase'), Breakdown: (BOMLine, 'breakdown')}


@transaction.atomic
def add_line(user, document, values, version, delete_id=None):
    document = lock(user, document, 'edit', version)
    if type(document) not in LINE_MODELS or document.status not in ('draft', 'rejected'):
        raise ValidationError('单据已冻结，不能再修改明细。')
    before = snapshot(document)
    model, key = LINE_MODELS[type(document)]
    allowed = {'name', 'room', 'specification', 'quantity', 'unit', 'unit_price'} if isinstance(document, Quote) else (
        {'material', 'quantity', 'waste'} if isinstance(document, Breakdown) else {'material', 'quantity', 'unit_price'})
    if set(values) - allowed:
        raise ValidationError('明细字段无效。')
    if delete_id:
        if not document.lines.filter(pk=delete_id).exists():
            raise ValidationError('明细不存在。')
        document.lines.filter(pk=delete_id).delete()
    else:
        if 'material' in values:
            material = values['material']
            require_access(user, material, 'view')
            if material.organization_id != document.organization_id:
                raise ValidationError('物料必须属于当前组织。')
        values = dict(values)
        for field in ('quantity', 'unit_price', 'waste'):
            if field in values:
                values[field] = number(values[field], places=3 if field == 'quantity' else (4 if field == 'waste' else 2),
                                       positive=field == 'quantity', nonnegative=field != 'quantity')
        line = model(**{key: document}, **values)
        line.full_clean()
        line.save()
    if isinstance(document, (Quote, Purchase)):
        total = sum((line.subtotal for line in document.lines.all()), Decimal('0'))
        document.total = (total * (document.discount if isinstance(document, Quote) else 1)).quantize(Decimal('.01'))
    save_version(document, version)
    audit(user, document, 'delete_line' if delete_id else 'add_line', before)
    return document
