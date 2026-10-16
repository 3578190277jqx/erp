from decimal import Decimal, ROUND_HALF_UP
from django.db import migrations


def backfill(apps,schema_editor):
    Order = apps.get_model('erp','Order')
    OrderLine = apps.get_model('erp','OrderLine')
    QuoteLine = apps.get_model('erp','QuoteLine')
    for order in Order.objects.using(schema_editor.connection.alias).filter(quote__isnull=False).select_related('quote').iterator():
        if OrderLine.objects.using(schema_editor.connection.alias).filter(order=order).exists():
            continue
        for line in QuoteLine.objects.using(schema_editor.connection.alias).filter(quote_id=order.quote_id):
            OrderLine.objects.using(schema_editor.connection.alias).create(order=order,source_quote_line_id=line.pk,
                name=line.name,room=line.room,specification=line.specification,quantity=line.quantity,unit=line.unit,
                unit_price=line.unit_price,line_total=(line.quantity*line.unit_price*order.quote.discount).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))


class Migration(migrations.Migration):
    dependencies = [('erp','0003_commercial_snapshots')]
    operations = [migrations.RunPython(backfill,migrations.RunPython.noop)]
