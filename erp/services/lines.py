from decimal import Decimal
from django.core.exceptions import ValidationError
from django.db import transaction
from erp.models import Quote, Purchase, Breakdown
from erp.line_metadata import LINE_SPECS
from erp.permissions import require_access
from .common import lock, snapshot, save_version, audit, number


@transaction.atomic
def add_line(user, document, values, version, delete_id=None):
    document = lock(user, document, 'edit', version)
    spec=LINE_SPECS.get(type(document))
    if spec is None or not spec.generic_editor or document.status not in ('draft', 'rejected'):
        raise ValidationError('单据已冻结，不能再修改明细。')
    before = snapshot(document)
    model, key = spec.model,spec.parent_field
    allowed = frozenset(spec.fields)
    if set(values) - allowed:
        raise ValidationError('明细字段无效。')
    if delete_id:
        line=document.lines.filter(pk=delete_id).first()
        if line is None:
            raise ValidationError('明细不存在。')
        before['line']=snapshot(line)
        line.delete()
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
        from .pricing import quote_amounts
        document.total = quote_amounts(document)['total'] if isinstance(document,Quote) else sum((line.subtotal for line in document.lines.all()),Decimal(0))
    save_version(document, version)
    after=snapshot(document)
    if not delete_id:
        line.refresh_from_db()
        after['line']=snapshot(line)
    audit(user, document, 'delete_line' if delete_id else 'add_line', before,after=after)
    return document
