import uuid
from django.db import migrations
from django.db.models import F


def backfill(apps,schema_editor):
    alias=schema_editor.connection.alias
    # V1.0 receive posted every line once, so do not receive this stock again.
    apps.get_model('erp','PurchaseLine').objects.using(alias).filter(purchase__status='received').update(received_qty=F('quantity'))
    # Adding a UUID column to old rows may use one effective default for all.
    # Each old design begins an independent series; new revisions retain it.
    Design=apps.get_model('erp','Design')
    for pk in Design.objects.using(alias).filter(previous_version__isnull=True).values_list('pk',flat=True).iterator():
        Design.objects.using(alias).filter(pk=pk).update(family_id=uuid.uuid4())


class Migration(migrations.Migration):
    dependencies=[('erp','0006_query_indexes')]
    operations=[migrations.RunPython(backfill,migrations.RunPython.noop)]
