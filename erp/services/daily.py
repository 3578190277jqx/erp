import csv
import io
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max
from erp import models as m
from erp.permissions import require_access,can,scoped
from .common import lock,audit,save_version


@transaction.atomic
def new_design_revision(user,design,version):
    design=lock(user,design,'edit',version)
    # Lock a stable root row to serialize revisions created from different versions.
    root=m.Design.objects.filter(family_id=design.family_id).order_by('id').first()
    m.Design.objects.select_for_update().get(pk=root.pk)
    highest=m.Design.objects.filter(family_id=design.family_id).aggregate(v=Max('revision'))['v'] or design.revision
    revised=m.Design.objects.create(organization=design.organization,owner=user,customer=design.customer,title=design.title,
                                    family_id=design.family_id,previous_version=design,revision=highest+1,note=design.note)
    save_version(design,design.version)
    audit(user,revised,'new_design_revision',after={'previous':design.code,'revision':revised.revision})
    return revised


IMPORT_COLUMNS={'customers':['name','phone','address','source'],'materials':['name','specification','unit','unit_cost','safety_stock']}


@transaction.atomic
def import_records(user,key,organization,file):
    if key not in IMPORT_COLUMNS or not can(user,key,'create',organization.pk):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    if file.size>1024*1024:
        raise ValidationError('导入文件最大1MB，最多1000行。')
    try:
        content=file.read().decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ValidationError('请将 Excel 另存为 UTF-8 CSV 后导入。')
    reader=csv.DictReader(io.StringIO(content))
    if reader.fieldnames != IMPORT_COLUMNS[key]:
        raise ValidationError('表头与模板不一致，请先下载模板。')
    model=m.Customer if key=='customers' else m.Material
    count=0
    for count,row in enumerate(reader,1):
        if count>1000 or None in row or any(v is None for v in row.values()):
            raise ValidationError(f'第{count}行列数错误或超过1000行。')
        row={k:v.strip() for k,v in row.items()}
        try:
            obj=model(organization=organization,owner=user,**row)
            obj.full_clean(exclude=['code']); obj.save()
        except (ValidationError,ValueError,TypeError) as exc:
            detail='；'.join(exc.messages) if isinstance(exc,ValidationError) else '数值格式错误'
            raise ValidationError(f'第{count}行：{detail}；整批未写入。')
    if not count:raise ValidationError('导入文件没有数据行。')
    m.AuditEvent.objects.create(organization=organization,owner=user,object_type='system',object_id=0,code='CSV-IMPORT',
                               action='import_csv',after={'module':key,'rows':count})
    return count
