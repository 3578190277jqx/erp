from django.conf import settings
from django.db import migrations, models
from django.db.models import F
import django.db.models.deletion


def populate_requester(apps, schema_editor):
    apps.get_model('erp', 'ChangeRequest').objects.using(schema_editor.connection.alias).update(requested_by=F('owner'))


class Migration(migrations.Migration):
    dependencies = [('erp', '0001_initial'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AddField(model_name='changerequest', name='requested_by',
                            field=models.ForeignKey(null=True, editable=False, on_delete=django.db.models.deletion.PROTECT,
                                                    related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='申请人')),
        migrations.RunPython(populate_requester, migrations.RunPython.noop),
        migrations.AlterField(model_name='changerequest', name='requested_by',
                              field=models.ForeignKey(editable=False, on_delete=django.db.models.deletion.PROTECT,
                                                      related_name='+', to=settings.AUTH_USER_MODEL, verbose_name='申请人')),
    ]
