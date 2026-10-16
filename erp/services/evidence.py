import hashlib
import logging
import re
from pathlib import Path
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import File
from django.db import transaction
from django.utils import timezone
from erp import models as m
from erp.permissions import scoped, require_access
from .common import audit,save_version,snapshot

MAX_BYTES = 20 * 1024 * 1024
PARENTS = {'customers':m.Customer,'measurements':m.Measurement,'designs':m.Design,'quotes':m.Quote,
           'orders':m.Order,'breakdowns':m.Breakdown,'deliveries':m.Delivery,'installations':m.Installation,'aftersales':m.AfterSales}


def valid_signature(extension, content):
    return {
        '.pdf':content.startswith(b'%PDF-'), '.png':content.startswith(b'\x89PNG\r\n\x1a\n'),
        '.jpg':content.startswith(b'\xff\xd8\xff'), '.jpeg':content.startswith(b'\xff\xd8\xff'),
        '.webp':content.startswith(b'RIFF') and content[8:12] == b'WEBP',
        '.dwg':bool(re.fullmatch(rb'AC10[0-9]{2}',content[:6])),
        '.dxf':bool(re.match(rb'\s*0\s*[\r\n]+\s*SECTION\s*[\r\n]+',content)) or content.startswith(b'AutoCAD Binary DXF'),
    }.get(extension,False)


def upload_attachment(user,parent,file,kind):
    if parent.module not in PARENTS:
        raise ValidationError('当前模块不支持附件。')
    require_access(user,parent,'edit')
    if file.size > MAX_BYTES or not file.size:
        raise ValidationError('文件必须为 1 字节至 20MB。')
    prefix=file.read(1024)
    name = str(file.name).replace('\\','/').split('/')[-1]
    if len(name)>200 or not valid_signature(Path(name).suffix.lower(),prefix):
        raise ValidationError('文件格式或内容不符合要求，仅支持 PDF、PNG、JPEG、WebP、DWG、DXF。')
    digest=hashlib.sha256();size=0;file.seek(0)
    while chunk:=file.read(64*1024):
        size+=len(chunk)
        if size>MAX_BYTES:raise ValidationError('文件超过20MB。')
        digest.update(chunk)
    if size!=file.size:raise ValidationError('文件大小不一致，请重新选择。')
    file.seek(0)
    item = m.Attachment(organization=parent.organization,owner=user,object_type=parent.module,object_id=parent.pk,
                        original_name=name,kind=kind,size=size,sha256=digest.hexdigest())
    try:
        with transaction.atomic():
            parent = type(parent).objects.select_for_update().get(pk=parent.pk)
            require_access(user,parent,'edit')
            item.file.save(name,File(file,name=name),save=False)
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
        item = m.Attachment.objects.get(pk=pk,deleted_at__isnull=True)
        parent_model = PARENTS[item.object_type]
    except (m.Attachment.DoesNotExist,KeyError):
        raise PermissionDenied('附件不可用。')
    if not scoped(user,parent_model).filter(pk=item.object_id,organization_id=item.organization_id).exists():
        raise PermissionDenied('附件不在当前授权范围。')
    return item


def purge_attachment_file(pk):
    item=m.Attachment.objects.get(pk=pk)
    if not item.deleted_at or item.purged_at:return False
    try:
        item.file.storage.delete(item.file.name)
        m.Attachment.objects.filter(pk=item.pk,deleted_at__isnull=False,purged_at__isnull=True).update(purged_at=timezone.now())
    except Exception:
        logging.getLogger(__name__).exception('Deleted attachment cleanup failed: id=%s',pk)
        return False
    return True


@transaction.atomic
def delete_attachment(user,item,version):
    parent_model=PARENTS.get(item.object_type)
    if not parent_model:raise PermissionDenied('附件不可用。')
    parent=parent_model.objects.select_for_update().get(pk=item.object_id)
    require_access(user,parent,'edit')
    item=m.Attachment.objects.select_for_update().get(pk=item.pk)
    if item.organization_id!=parent.organization_id:raise PermissionDenied('附件组织不匹配。')
    try:expected=int(version)
    except (ValueError,TypeError):raise ValidationError('缺少有效版本。')
    if item.deleted_at or item.version!=expected:raise ValidationError('附件已删除或版本变化，请刷新。')
    before=snapshot(item);item.deleted_at=timezone.now();save_version(item,expected)
    audit(user,parent,'delete_attachment',before=before,after={'attachment':item.code,'sha256':item.sha256,'deleted_at':str(item.deleted_at)})
    transaction.on_commit(lambda:purge_attachment_file(item.pk))
