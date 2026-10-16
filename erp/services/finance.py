from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from erp.models import Order, Purchase, Payment
from erp.permissions import can, require_access
from .common import number, audit, save_version


@transaction.atomic
def record_payment(user, document, amount, reference, paid_date, method, note):
    if not isinstance(document, (Order, Purchase)):
        raise ValidationError('收付款来源单据无效。')
    require_access(user, document, 'view')
    if not can(user, 'finance', 'create', document.organization_id):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied('只有财务可登记收付款。')
    document = type(document).objects.select_for_update().get(pk=document.pk)
    value = number(amount, positive=True)
    reference = str(reference).strip()
    if not reference or Payment.objects.filter(reference=reference).exists():
        raise ValidationError('凭证号不能为空或重复。')
    incoming = isinstance(document, Order)
    if incoming and document.status in ('draft', 'cancelled'):
        raise ValidationError('只有已确认且未取消的订单可登记收款。')
    if not incoming and document.status not in ('ordered', 'received'):
        raise ValidationError('采购下单后才能付款。')
    key = 'order' if incoming else 'purchase'
    paid = Payment.objects.filter(**{key: document}).aggregate(total=Sum('amount'))['total'] or 0
    if paid + value > document.total:
        raise ValidationError(f'本次金额超过未结余额 {document.total - paid:.2f}。')
    # Serialize payments on their source document, including SQLite CAS.
    save_version(document, document.version)
    payment = Payment(organization=document.organization, owner=user, direction='in' if incoming else 'out',
                      amount=value, reference=reference, paid_date=paid_date, method=method,
                      note=note, **{key: document})
    payment.full_clean(exclude=['code'])
    payment.save()
    audit(user, payment, 'record_payment')
    return payment
