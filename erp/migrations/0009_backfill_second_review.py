from django.db import migrations
from django.db.models import F


def backfill(apps,schema_editor):
    alias=schema_editor.connection.alias
    Order=apps.get_model('erp','Order')
    Customer=apps.get_model('erp','Customer')
    Change=apps.get_model('erp','ChangeRequest')
    Audit=apps.get_model('erp','AuditEvent')
    for order in Order.objects.using(alias).filter(quote__isnull=False).select_related('quote').iterator():
        Order.objects.using(alias).filter(pk=order.pk).update(discount=order.quote.discount)
    # Pending v1.1 line edits used different pricing semantics. Preserve their
    # rows, but require a fresh request instead of approving an ambiguous amount.
    for change in Change.objects.using(alias).filter(status='pending',lines__isnull=False).distinct():
        Change.objects.using(alias).filter(pk=change.pk).update(status='rejected',version=F('version')+1)
        Audit.objects.using(alias).create(organization_id=change.organization_id,owner_id=change.owner_id,
            object_type='changes',object_id=change.pk,code=change.code,action='upgrade_change_review',
            before={'status':'pending'},after={'status':'rejected','reason':'计价规则升级，请核对后重新申请增减项。','migration':'0009'})
    partners={}
    for order in Order.objects.using(alias).filter(kind='manufacturing',source_handoff__isnull=False).select_related('customer','source_handoff').order_by('id'):
        old=order.customer
        if old.source!='门厂交接' or old.phone!='内部协作':continue
        key=(order.organization_id,order.source_handoff.organization_id)
        Customer.objects.using(alias).filter(pk=old.pk).update(is_internal=True)
        if key not in partners:
            partners[key]=old.pk
            Customer.objects.using(alias).filter(pk=old.pk).update(internal_source_id=key[1])
        elif old.pk!=partners[key]:
            Order.objects.using(alias).filter(pk=order.pk).update(customer_id=partners[key])
            Audit.objects.using(alias).create(organization_id=order.organization_id,owner_id=order.owner_id,
                object_type='orders',object_id=order.pk,code=order.code,action='upgrade_internal_partner',
                before={'customer':old.pk},after={'customer':partners[key],'migration':'0009'})
    # Routes are deliberately NOT inferred from historical sends. An admin must
    # explicitly authorize partner factories, including after the upgrade.


class Migration(migrations.Migration):
    dependencies=[('erp','0008_second_review_pricing_collaboration')]
    operations=[migrations.RunPython(backfill,migrations.RunPython.noop)]
