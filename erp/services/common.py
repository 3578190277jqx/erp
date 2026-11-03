from decimal import Decimal, InvalidOperation
from django.core.exceptions import ValidationError
from django.db.models import F
from erp.models import AuditEvent
from erp.permissions import require_access
from erp.audit_types import audit_type_for


def number(value, places=2, positive=False, nonnegative=False):
    try:
        result = Decimal(str(value))
        if not result.is_finite() or abs(result) >= Decimal('1000000000'):
            raise InvalidOperation
        quantum = Decimal('1').scaleb(-places)
        if result != result.quantize(quantum):
            raise ValidationError(f'数值最多保留 {places} 位小数。')
        result = result.quantize(quantum)
    except (InvalidOperation, ValueError, TypeError):
        raise ValidationError('请输入有效且未超出范围的数值。')
    if positive and result <= 0:
        raise ValidationError('数值必须大于零。')
    if nonnegative and result < 0:
        raise ValidationError('数值不能为负数。')
    return result


def snapshot(obj):
    data = {}
    for field in obj._meta.fields:
        if field.name in ('created_at', 'updated_at'):
            continue
        value = getattr(obj, field.attname)
        if value is not None:
            data[field.name] = str(value)
    return data


def audit(user, obj, action, before=None, after=None):
    return AuditEvent.objects.create(organization=obj.organization, owner=user,
                                    object_type=audit_type_for(obj), object_id=obj.pk, code=obj.code,
                                    action=action, before=before or {}, after=after if after is not None else snapshot(obj))


def lock(user, obj, action, version):
    require_access(user, obj, action)
    locked = type(obj).objects.select_for_update().get(pk=obj.pk)
    try:
        expected = int(version)
    except (ValueError, TypeError):
        raise ValidationError('缺少有效版本号，请刷新页面。')
    if locked.version != expected:
        raise ValidationError('数据已被其他操作更新，请刷新后重试。')
    return locked


def save_version(obj, expected):
    # Compare-and-swap is also enforced on SQLite, where row locks are not available.
    if type(obj).objects.filter(pk=obj.pk, version=expected).update(version=F('version') + 1) != 1:
        raise ValidationError('数据已更新，请刷新后重试。')
    obj.version = expected + 1
    obj.full_clean(exclude=['code'])
    obj.save()


def system_update(user, obj, action, **changes):
    current = type(obj).objects.select_for_update().get(pk=obj.pk)
    before = snapshot(current)
    expected = current.version
    for key, value in changes.items():
        setattr(current, key, value)
    save_version(current, expected)
    audit(user, current, action, before)
    return current
