"""Read-only audit presentation; stored snapshots remain authoritative."""
import json
from collections import defaultdict
from django.contrib.auth import get_user_model
from django.core.exceptions import FieldDoesNotExist
from django.db import models
from django.db.models import Q
from . import models as m
from .permissions import scoped, accessible_organizations
from .registry import MODULES

LABELS = {
    'reason':'停止原因','attachment':'附件编号','attachments':'附件快照','sha256':'文件校验和',
    'policy':'处理规则','recalculated_against_order_version':'拟额计算订单版本',
    'attempted_order_version':'尝试计算订单版本','recalculation_error':'重算未完成原因',
    'change':'变更申请快照','handoff':'交接快照','handoffs':'交接快照',
    'route_override':'管理员例外上传','target':'目标工厂','source_code':'源订单编号',
    'source_version':'源订单版本','lines':'产品明细','id':'记录 ID','name':'名称',
    'line':'申请明细快照',
}
WRAPPERS = {'change':m.ChangeRequest,'handoff':m.Handoff,'handoffs':m.Handoff,
            'attachment':m.Attachment,'attachments':m.Attachment}
MISSING = object()


def field_for(model,key):
    if model is None:return None
    try:return model._meta.get_field(key)
    except FieldDoesNotExist:return None


def flatten(value,model,path=(),labels=(),field=None):
    if isinstance(value,dict) and value:
        for key,child in value.items():
            key=str(key);child_field=field_for(model,key)
            label=LABELS.get(key) or str(getattr(child_field,'verbose_name',key))
            child_model=WRAPPERS.get(key)
            if key=='line' and model is m.ChangeRequest:child_model=m.ChangeLine
            if child_model is None and child_field is not None and child_field.is_relation:
                child_model=child_field.related_model
            yield from flatten(child,child_model,path+(key,),labels+(label,),child_field)
    elif isinstance(value,list) and value:
        for index,child in enumerate(value):
            yield from flatten(child,model,path+(index,),labels+(f'第{index+1}项',),field)
    elif path:
        yield {'path':path,'key':' / '.join(labels),'raw':value,'field':field}


def reference(row):
    field=row['field'];value=row['raw']
    if not isinstance(field,models.ForeignKey) or isinstance(value,bool):return None
    raw=str(value)
    if not raw.isascii() or not raw.isdigit() or not 0<int(raw)<2**63:return None
    return field.related_model,int(raw)


def reference_names(user,event,organization,rows):
    """One query per reference model, never one query per nested row."""
    wanted=defaultdict(set)
    for row in rows:
        ref=reference(row)
        if ref:wanted[ref[0]].add(ref[1])
    names={};User=get_user_model()
    for model,ids in wanted.items():
        if model is m.Organization:
            qs=accessible_organizations(user)
            if organization:qs=qs.filter(pk=organization)
        elif model is User:
            # Only current colleagues of the audited organization, or the
            # already-visible event actor. Never enumerate all account names.
            qs=User.objects.filter(Q(memberships__organization_id=event.organization_id)|Q(pk=event.owner_id)).distinct()
        elif getattr(model,'module',None) in MODULES:
            qs=scoped(user,model,organization)
        else:continue
        for obj in qs.filter(pk__in=ids):
            names[(model,obj.pk)]=(obj.first_name or obj.username) if model is User else str(obj)
    return names


def display(row,names):
    if row is None:return '未记录此字段'
    value=row['raw']
    if value is None:return '未记录'
    if value=='':return '未填写'
    if isinstance(value,bool):return '是' if value else '否'
    if value==[]:return '无条目'
    if value=={}:return '无字段'
    ref=reference(row)
    if ref:
        name=names.get(ref)
        return f'{name}（ID: {ref[1]}）' if name else f'ID: {ref[1]}（当前名称不可用）'
    field=row['field']
    if field is not None:
        for stored,label in getattr(field,'flatchoices',()):
            if str(stored)==str(value):return str(label)
        if isinstance(field,models.BooleanField) and value in ('True','False'):
            return '是' if value=='True' else '否'
    return str(value)


def audit_context(user,event,organization=None):
    module=MODULES.get(event.object_type);model=module.model if module else None
    stages=[]
    for title,value in [('变更前',event.before),('变更后',event.after)]:
        if not isinstance(value,(dict,list)):value={'内容':value}
        stages.append((title,list(flatten(value,model))))
    names=reference_names(user,event,organization,[row for _,rows in stages for row in rows])
    snapshots=[{'title':title,'rows':[dict(row,value=display(row,names)) for row in rows]} for title,rows in stages]
    before={row['path']:row for row in stages[0][1]};after={row['path']:row for row in stages[1][1]}
    differences=[]
    for path in dict.fromkeys([*before,*after]):
        old=before.get(path);new=after.get(path)
        a=old['raw'] if old else MISSING;b=new['raw'] if new else MISSING
        # bool is a subclass of int: False -> 0 must remain visible.
        if type(a) is type(b) and a==b:continue
        differences.append({'key':(new or old)['key'],'before':display(old,names),'after':display(new,names)})
    return {'audit_payloads':snapshots,'audit_differences':differences,
            'audit_raw':json.dumps({'before':event.before,'after':event.after},ensure_ascii=False,indent=2)}
