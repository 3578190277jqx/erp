from django.db import migrations


def backfill(apps,schema_editor):
    Order=apps.get_model('erp','Order')
    # A missing quote never proves an implicit historical discount. Preserve all
    # prices and totals; a human can declare the basis through approved change.
    Order.objects.using(schema_editor.connection.alias).filter(quote__isnull=False).update(discount_confirmed=True)
    Order.objects.using(schema_editor.connection.alias).filter(kind='manufacturing').update(discount_confirmed=True)


class Migration(migrations.Migration):
    dependencies=[('erp','0014_contract_basis_and_plan_revision')]
    operations=[migrations.RunPython(backfill,migrations.RunPython.noop)]
