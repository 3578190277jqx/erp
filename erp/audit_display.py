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
from .time_display import local_timestamp

LABELS = {
    'reason':'停止原因','attachment':'附件编号','attachments':'附件快照','sha256':'文件校验和',
    'policy':'处理规则','recalculated_against_order_version':'拟额计算订单版本',
    'attempted_order_version':'尝试计算订单版本','recalculation_error':'重算未完成原因',
    'change':'变更申请快照','handoff':'交接快照','handoffs':'交接快照',
    'route_override':'管理员例外上传','target':'目标工厂','source_code':'源订单编号',
    'source_version':'源订单版本','lines':'产品明细','id':'记录 ID','name':'名称',
    'line':'申请明细快照',
    'archived_at':'停止共享时间','deleted_at':'删除时间',
    'attachment_state':'附件状态快照',
}
WRAPPERS = {'change':m.ChangeRequest,'handoff':m.Handoff,'handoffs':m.Handoff,
            'attachment':m.Attachment,'attachments':m.Attachment,'attachment_state':m.Attachment}
TIME_KEYS = {'archived_at','deleted_at','purged_at','created_at','updated_at'}


def field_for(model,key):
    if model is None:return None
    if model is m.Handoff and key=='target':key='target_organization'
    try:return model._meta.get_field(key)
    except FieldDoesNotExist:return None


def child_metadata(model,key):
    field=field_for(model,key)
    label=LABELS.get(key) or str(getattr(field,'verbose_name',key))
    if model is m.Attachment and key=='archived_at':label='归档时间'
    child_model=WRAPPERS.get(key)
    if key=='line' and model is m.ChangeRequest:child_model=m.ChangeLine
    if child_model is None and field is not None and field.is_relation:
        child_model=field.related_model
    return field,label,child_model


def flatten(value,model,path=(),labels=(),field=None,summary_lists=False):
    if isinstance(value,dict) and value:
        for key,child in value.items():
            key=str(key);child_field,label,child_model=child_metadata(model,key)
            yield from flatten(child,child_model,path+(key,),labels+(label,),child_field,summary_lists)
    elif isinstance(value,list) and value and not summary_lists:
        for index,child in enumerate(value):
            yield from flatten(child,model,path+(index,),labels+(f'第{index+1}项',),field)
    elif path or isinstance(value,list) and summary_lists:
        yield {'path':path,'key':' / '.join(labels) or '内容','raw':value,'field':field,
               'list_summary':summary_lists and isinstance(value,list)}


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
            allowed=Q(pk__in=accessible_organizations(user).values('pk'))
            if event.object_type=='handoffs':
                # Authorize from the real visible relationship, never snapshot IDs.
                handoff=scoped(user,m.Handoff,organization).filter(
                    pk=event.object_id,organization_id=event.organization_id).values(
                    'organization_id','target_organization_id').first()
                if handoff:allowed|=Q(pk__in=handoff.values())
            qs=m.Organization.objects.filter(allowed)
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
    value=row['raw']
    if value is None:return '未记录'
    if value=='':return '未填写'
    if isinstance(value,bool):return '是' if value else '否'
    if row.get('list_summary'):
        return f'{len(value)} 条（内容见完整快照）' if value else '0 条（无条目）'
    if value==[]:return '无条目'
    if value=={}:return '无字段'
    ref=reference(row)
    if ref:
        name=names.get(ref)
        return f'{name}（ID: {ref[1]}）' if name else f'ID: {ref[1]}（当前名称不可用）'
    field=row['field']
    path=row.get('path',())
    if isinstance(field,models.DateTimeField) or path and path[-1] in TIME_KEYS:
        formatted=local_timestamp(value)
        if formatted is not None:return formatted
    if field is not None:
        for stored,label in getattr(field,'flatchoices',()):
            if str(stored)==str(value):return str(label)
        if isinstance(field,models.BooleanField) and value in ('True','False'):
            return '是' if value=='True' else '否'
    return str(value)


def same_value(a,b):
    """JSON equality with type fidelity, including nested False versus zero."""
    if type(a) is not type(b):return False
    if isinstance(a,dict):return a.keys()==b.keys() and all(same_value(a[k],b[k]) for k in a)
    if isinstance(a,list):return len(a)==len(b) and all(same_value(x,y) for x,y in zip(a,b))
    return a==b


def compare_payloads(before,after,model,names):
    differences=[];recorded=[]

    def record(stage,value,model,path,labels,field):
        recorded.extend(dict(row,stage=stage,value=display(row,names))
                        for row in flatten(value,model,path,labels,field,summary_lists=True))

    def compare(a,b,model,path=(),labels=(),field=None):
        if same_value(a,b):return
        if isinstance(a,dict) and isinstance(b,dict):
            for key in dict.fromkeys([*a,*b]):
                child_field,label,child_model=child_metadata(model,str(key))
                child_path=path+(str(key),);child_labels=labels+(label,)
                if key in a and key in b:
                    compare(a[key],b[key],child_model,child_path,child_labels,child_field)
                elif key in a:record('变更前',a[key],child_model,child_path,child_labels,child_field)
                else:record('变更后',b[key],child_model,child_path,child_labels,child_field)
        elif isinstance(a,list) and isinstance(b,list):
            # No stable item identity is guaranteed in historical snapshots.
            # One collection comparison avoids false index-based field removals.
            differences.append({'key':' / '.join(labels) or '内容',
                'before':f'{len(a)} 条（内容见完整快照）' if a else '0 条（无条目）',
                'after':(f'{len(b)} 条（条数不变，内容或顺序有变化；见完整快照）' if len(a)==len(b)
                         else f'{len(b)} 条（内容见完整快照）' if b else '0 条（已清空）')})
        elif isinstance(a,(dict,list)) or isinstance(b,(dict,list)):
            record('变更前',a,model,path,labels,field)
            record('变更后',b,model,path,labels,field)
        else:
            old={'raw':a,'field':field,'path':path};new={'raw':b,'field':field,'path':path}
            differences.append({'key':' / '.join(labels),'before':display(old,names),'after':display(new,names)})

    compare(before,after,model)
    return differences,recorded


def audit_context(user,event,organization=None):
    module=MODULES.get(event.object_type);model=module.model if module else None
    stages=[];values=[]
    for title,value in [('变更前',event.before),('变更后',event.after)]:
        if not isinstance(value,(dict,list)):value={'内容':value}
        values.append(value)
        stages.append((title,list(flatten(value,model))))
    names=reference_names(user,event,organization,[row for _,rows in stages for row in rows])
    snapshots=[{'title':title,'rows':[dict(row,value=display(row,names)) for row in rows]} for title,rows in stages]
    differences,recorded=compare_payloads(*values,model,names)
    is_write=not values[0]
    write_rows=[dict(row,value=display(row,names)) for row in flatten(values[1],model,summary_lists=True)] if is_write else []

    def covers(rows,full):
        left={r['path']:r['raw'] for r in rows};right={r['path']:r['raw'] for r in full}
        return bool(left) and same_value(left,right)

    recorded_payloads=[]
    for index,payload in enumerate(snapshots):
        rows=[r for r in recorded if r['stage']==payload['title']]
        complete=covers(rows,payload['rows'])
        payload['show']=bool(payload['rows']) and not complete and not (is_write and index==1 and covers(write_rows,payload['rows']))
        if rows:recorded_payloads.append({'title':payload['title'],'rows':rows,'complete':complete})
    after_count=sum(r['stage']=='变更后' for r in recorded)
    heading='本次写入' if is_write else '本次变更' if differences or not recorded else '本次操作记录'
    if is_write:summary=f'{len(write_rows)} 个记录字段'
    elif differences:
        summary=f'{len(differences)} 项可比变化'+(f' · {after_count} 个补充字段' if after_count else '')
    elif recorded:summary=f'{after_count} 个结果记录字段' if after_count else f'{len(recorded)} 个历史记录字段'
    else:summary='0 项可比变化'
    return {'audit_payloads':snapshots,'audit_differences':differences,
            'audit_is_write':is_write,'audit_write_rows':write_rows,'audit_heading':heading,'audit_summary':summary,
            'audit_recorded_fields':recorded,
            'audit_recorded_payloads':list(reversed(recorded_payloads)),
            'audit_raw':json.dumps({'before':event.before,'after':event.after},ensure_ascii=False,indent=2)}
