import hashlib
from pathlib import Path
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from erp import models as m
from erp.permissions import scoped, require_access
from .common import audit

MAX_BYTES = 20 * 1024 * 1024
PARENTS = {'customers':m.Customer,'measurements':m.Measurement,'designs':m.Design,'quotes':m.Quote,
           'orders':m.Order,'breakdowns':m.Breakdown,'deliveries':m.Delivery,'installations':m.Installation,'aftersales':m.AfterSales}


def valid_signature(extension, content):
    return {
        '.pdf':content.startswith(b'%PDF-'), '.png':content.startswith(b'\x89PNG\r\n\x1a\n'),
        '.jpg':content.startswith(b'\xff\xd8\xff'), '.jpeg':content.startswith(b'\xff\xd8\xff'),
        '.webp':content.startswith(b'RIFF') and content[8:12] == b'WEBP',
        '.dwg':content.startswith(b'AC10'),
        '.dxf':b'SECTION' in content[:1024] or content.startswith(b'AutoCAD Binary DXF'),
    }.get(extension,False)


def upload_attachment(user,parent,file,kind):
    if parent.module not in PARENTS:
        raise ValidationError('当前模块不支持附件。')
    require_access(user,parent,'edit')
    if file.size > MAX_BYTES or not file.size:
        raise ValidationError('文件必须为 1 字节至 20MB。')
    content = file.read(MAX_BYTES+1)
    name = str(file.name).replace('\\','/').split('/')[-1]
    if len(name)>200 or len(content)>MAX_BYTES or not valid_signature(Path(name).suffix.lower(),content):
        raise ValidationError('文件格式或内容不符合要求，仅支持 PDF、PNG、JPEG、WebP、DWG、DXF。')
    file.seek(0)
    item = m.Attachment(organization=parent.organization,owner=user,object_type=parent.module,object_id=parent.pk,
                        original_name=name,kind=kind,size=len(content),sha256=hashlib.sha256(content).hexdigest())
    try:
        with transaction.atomic():
            item.file.save(name,file,save=False)
            item.full_clean(exclude=['code'])
            item.save()
            audit(user,parent,'upload_attachment',after={'attachment':item.code,'name':name,'sha256':item.sha256})
    except Exception:
        if item.file.name:
            item.file.delete(save=False)
        raise
    return item


def authorized_attachment(user,pk):
    try:
        item = m.Attachment.objects.get(pk=pk)
        parent_model = PARENTS[item.object_type]
    except (m.Attachment.DoesNotExist,KeyError):
        raise PermissionDenied('附件不可用。')
    if not scoped(user,parent_model).filter(pk=item.object_id,organization_id=item.organization_id).exists():
        raise PermissionDenied('附件不在当前授权范围。')
    return item
