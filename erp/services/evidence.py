from dataclasses import dataclass
import hashlib
import logging
import re
from pathlib import Path
from django.core.exceptions import PermissionDenied, ValidationError, ObjectDoesNotExist
from django.core.files.base import File
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from erp import models as m
from erp.permissions import scoped, require_access, can, memberships, member_orgs, role_allows
from .common import audit,save_version,snapshot

MAX_BYTES = 20 * 1024 * 1024
PARENTS = {'customers':m.Customer,'measurements':m.Measurement,'designs':m.Design,'quotes':m.Quote,
           'orders':m.Order,'breakdowns':m.Breakdown,'deliveries':m.Delivery,'installations':m.Installation,'aftersales':m.AfterSales,
           'purchases':m.Purchase,'production':m.Production,'changes':m.ChangeRequest,
           'handoffs':m.Handoff,'receipts':m.PurchaseReceipt}


def valid_signature(extension, content):
    return {
        '.pdf':content.startswith(b'%PDF-'), '.png':content.startswith(b'\x89PNG\r\n\x1a\n'),
        '.jpg':content.startswith(b'\xff\xd8\xff'), '.jpeg':content.startswith(b'\xff\xd8\xff'),
        '.webp':content.startswith(b'RIFF') and content[8:12] == b'WEBP',
        '.dwg':bool(re.fullmatch(rb'AC10[0-9]{2}',content[:6])),
        '.dxf':bool(re.match(rb'\s*0\s*[\r\n]+\s*SECTION\s*[\r\n]+',content)) or content.startswith(b'AutoCAD Binary DXF'),
    }.get(extension,False)


@dataclass(frozen=True)
class EvidencePermissions:
    visible: bool=False
    write: bool=False
    archived_read: bool=False
    manage_sharing: bool=False
    upload: bool=False
    receiver_stop: bool=False
    manage_factory_sharing: bool=False


def evidence_permissions(user,parent):
    visible=scoped(user,type(parent)).filter(pk=parent.pk).exists()
    if not visible:return EvidencePermissions()
    if isinstance(parent,m.Handoff):
        from .collaboration import route_enabled
        source_read=scoped(user,m.Order).filter(pk=parent.order_id).exists()
        source_edit=scoped(user,m.Order,action='edit').filter(pk=parent.order_id).exists()
        receiver_actor=can(user,'handoffs','accept',parent.target_organization_id)
        receiver=parent.status in ('sent','accepted','completed') and receiver_actor
        enabled=route_enabled(user,parent)
        write=source_edit or receiver and enabled
        # The management membership itself must cover the source order; a
        # broader sales membership cannot lend its data scope to manager/own.
        manage=user.is_superuser or source_edit and any(
            role_allows(member.role,'handoffs','stop_share')
            and parent.order.organization_id in member_orgs(member)
            and (member.scope!='own' or parent.order.owner_id==user.pk)
            for member in memberships(user))
        factory_manage=any(member.role in ('boss','manager') and member.scope!='own'
            and parent.target_organization_id in member_orgs(member) for member in memberships(user))
        return EvidencePermissions(True,write,source_read,manage,
                                   upload=write and enabled and parent.status in ('sent','accepted','completed'),
                                   receiver_stop=receiver_actor,manage_factory_sharing=factory_manage)
    if isinstance(parent,m.PurchaseReceipt):
        write=scoped(user,m.Purchase,action='receive').filter(pk=parent.purchase_id).exists()
    elif isinstance(parent,m.Production) and scoped(user,m.Production,action='issue').filter(pk=parent.pk).exists():write=True
    else:write=scoped(user,type(parent),action='edit').filter(pk=parent.pk).exists()
    return EvidencePermissions(True,write,upload=write)


def can_write_evidence(user,parent):
    return evidence_permissions(user,parent).write


def can_delete_evidence(user,parent,item,permissions=None):
    rights=permissions or evidence_permissions(user,parent)
    if item.archived_at and not rights.archived_read:return False
    if isinstance(parent,m.Handoff) and item.owner_id!=user.pk and not user.is_superuser:return False
    return rights.write


def can_stop_sharing(user,parent,item,permissions=None):
    rights=permissions or evidence_permissions(user,parent)
    if not isinstance(parent,m.Handoff) or not rights.visible or item.archived_at or item.deleted_at:return False
    own=item.owner_id==user.pk
    return (rights.write and (own or rights.manage_sharing)) or (rights.receiver_stop and (
        own or rights.manage_factory_sharing and item.uploaded_from_organization_id==parent.target_organization_id))


def require_evidence_write(user,parent):
    if not can_write_evidence(user,parent):raise PermissionDenied('没有此单据的附件维护权限。')


def require_evidence_upload(user,parent):
    if not evidence_permissions(user,parent).upload:raise PermissionDenied('没有附件上传权，协作已暂停或交接需重新发送。')


def evidence_epoch(parent):
    return m.EvidenceState.objects.filter(organization_id=parent.organization_id,object_type=parent.module,
                    object_id=parent.pk).values_list('epoch',flat=True).first() or 0


def advance_evidence_epoch(parent):
    """Caller holds the parent row lock; never mutate business version/data."""
    state,_=m.EvidenceState.objects.get_or_create(object_type=parent.module,object_id=parent.pk,
                      defaults={'organization_id':parent.organization_id})
    if state.organization_id!=parent.organization_id:raise PermissionDenied('附件版本所属组织不匹配。')
    if m.EvidenceState.objects.filter(pk=state.pk,epoch=state.epoch).update(epoch=F('epoch')+1,updated_at=timezone.now())!=1:
        raise ValidationError('附件集合已变化，请刷新后重试。')


def upload_attachment(user,parent,file,kind,share_confirmed=False,parent_version=None,target_id=None):
    if parent.module not in PARENTS:
        raise ValidationError('当前模块不支持附件。')
    require_evidence_upload(user,parent)
    if isinstance(parent,m.Handoff) and (not share_confirmed or kind not in ('drawing','photo')):
        raise ValidationError('交接附件须明确确认已脱敏，仅分享生产图纸或现场照片；禁止上传合同凭证。')
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
            require_evidence_upload(user,parent)
            item.organization=parent.organization
            item.uploaded_from_organization_id=(parent.target_organization_id if isinstance(parent,m.Handoff)
                and not scoped(user,m.Order,action='edit').filter(pk=parent.order_id).exists() else parent.organization_id)
            if isinstance(parent,m.Handoff):
                try:expected_version,expected_target=int(parent_version),int(target_id)
                except (ValueError,TypeError):raise ValidationError('交接附件缺少有效版本与目标工厂，请刷新后重试。')
                if parent.version!=expected_version or parent.target_organization_id!=expected_target:
                    raise ValidationError('交接版本或目标工厂已变化，请重新核对共享范围后上传。')
            item.file.save(name,File(file,name=name),save=False)
            item.full_clean(exclude=['code'])
            item.save()
            advance_evidence_epoch(parent)
            payload={'attachment':item.code,'name':name,'sha256':item.sha256}
            if isinstance(parent,m.Handoff) and user.is_superuser and not m.CollaborationRoute.objects.filter(
                    source_id=parent.organization_id,target_id=parent.target_organization_id,enabled=True).exists():
                payload['route_override']=True
            audit(user,parent,'upload_attachment',after=payload)
    except Exception:
        if item.file.name:
            item.file.delete(save=False)
        raise
    return item


def _same_version(parent):
    # Re-read after metadata/permissions: using the initial epoch here would
    # miss a stop/delete committed while this request was reading the files.
    return (type(parent).objects.filter(pk=parent.pk,version=parent.version).exists()
            and evidence_epoch(parent)==parent._evidence_epoch)


def attachment_context(user,parent):
    """One snapshot matching the rendered parent; a new version needs refresh."""
    try:current=type(parent).objects.get(pk=parent.pk)
    except ObjectDoesNotExist:return [],EvidencePermissions(),True
    if current.version!=parent.version:return [],EvidencePermissions(),True
    current._evidence_epoch=evidence_epoch(current)
    rights=evidence_permissions(user,current)
    if not rights.visible:return [],rights,False
    files=m.Attachment.objects.filter(organization=current.organization,object_type=current.module,
              object_id=current.pk,deleted_at__isnull=True).select_related('owner')
    if not rights.archived_read:files=files.filter(archived_at__isnull=True)
    files=list(files)
    if _same_version(current):return files,rights,False
    return [],EvidencePermissions(),True


def authorized_attachment(user,pk):
    for _ in range(2):
        try:
            identity=m.Attachment.objects.get(pk=pk,deleted_at__isnull=True)
            parent_model=PARENTS[identity.object_type]
            parent=parent_model.objects.get(pk=identity.object_id)
            parent._evidence_epoch=evidence_epoch(parent)
            rights=evidence_permissions(user,parent)
            item=m.Attachment.objects.get(pk=pk,deleted_at__isnull=True)
        except (ObjectDoesNotExist,KeyError):raise PermissionDenied('附件不可用。')
        if item.object_type!=identity.object_type or item.object_id!=parent.pk or item.organization_id!=parent.organization_id:
            raise PermissionDenied('附件关联已变化。')
        if not rights.visible or item.archived_at and not rights.archived_read:
            raise PermissionDenied('附件不在当前授权范围，归档仅源门店可查阅。')
        if _same_version(parent):return item
    raise PermissionDenied('共享范围正在变化，请刷新后重试。')


@transaction.atomic
def stop_sharing_attachment(user,item,version,parent_version,reason):
    if item.object_type!='handoffs':raise ValidationError('只有交接附件支持停止共享。')
    parent=m.Handoff.objects.select_for_update().get(pk=item.object_id)
    item=m.Attachment.objects.select_for_update().get(pk=item.pk)
    rights=evidence_permissions(user,parent)
    if item.object_type!='handoffs' or item.object_id!=parent.pk or item.organization_id!=parent.organization_id:
        raise PermissionDenied('附件关联不匹配。')
    if not can_stop_sharing(user,parent,item,rights):
        if item.deleted_at or item.archived_at:raise ValidationError('附件已停止共享或已删除，请刷新。')
        raise PermissionDenied('没有此附件的停止共享权限。')
    reason=(reason or '').strip()
    if not reason or len(reason)>5000:raise ValidationError('请填写1至5000字的停止共享原因。')
    try:expected,expected_parent=int(version),int(parent_version)
    except (ValueError,TypeError):raise ValidationError('缺少有效附件与交接版本。')
    if item.deleted_at or item.archived_at or item.version!=expected or parent.version!=expected_parent:
        raise ValidationError('附件已停止共享、已删除或版本变化，请刷新。')
    before={'handoff':snapshot(parent),'attachment':snapshot(item)}
    item.archived_at=timezone.now();item.archive_reason='stopped'
    save_version(item,expected);advance_evidence_epoch(parent)
    audit(user,parent,'stop_sharing_attachment',before,after={'attachment':item.code,'reason':reason,
          'archived_at':str(item.archived_at),'sha256':item.sha256,'policy':'文件保留，仅源门店可查阅，不自动恢复共享。'})
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
    require_evidence_write(user,parent)
    item=m.Attachment.objects.select_for_update().get(pk=item.pk)
    if item.organization_id!=parent.organization_id:raise PermissionDenied('附件组织不匹配。')
    if not can_delete_evidence(user,parent,item):raise PermissionDenied('交接附件仅上传者或系统管理员可删除，并须保留当前单据维护权限。')
    try:expected=int(version)
    except (ValueError,TypeError):raise ValidationError('缺少有效版本。')
    if item.deleted_at or item.version!=expected:raise ValidationError('附件已删除或版本变化，请刷新。')
    before=snapshot(item);item.deleted_at=timezone.now();save_version(item,expected)
    advance_evidence_epoch(parent)
    audit(user,parent,'delete_attachment',before=before,after={'attachment':item.code,'sha256':item.sha256,'deleted_at':str(item.deleted_at)})
    transaction.on_commit(lambda:purge_attachment_file(item.pk))
