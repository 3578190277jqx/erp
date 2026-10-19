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
from .audit_values import same_value, identity, list_identity, canonical_rows
from .audit_types import audit_subject, audit_type_title, normalize_stage
from .line_metadata import LINE_SPECS, ROW_SPECS

LABELS = {
    'reason':'停止原因','attachment':'附件编号','attachments':'附件快照','sha256':'文件校验和',
    'policy':'处理规则','recalculated_against_order_version':'拟额计算订单版本',
    'attempted_order_version':'尝试计算订单版本','recalculation_error':'重算未完成原因',
    'change':'变更申请快照','handoff':'交接快照','handoffs':'交接快照',
    'route_override':'管理员例外上传','target':'目标工厂','source_code':'源订单编号',
    'source_version':'源订单版本','lines':'产品明细','id':'记录 ID','name':'名称',
    'line':'明细快照','plans':'分期计划','order':'合同订单',
    'archived_at':'停止共享时间','deleted_at':'删除时间',
    'attachment_state':'附件状态快照',
}
WRAPPERS = {'change':m.ChangeRequest,'handoff':m.Handoff,'handoffs':m.Handoff,
            'attachment':m.Attachment,'attachments':m.Attachment,'attachment_state':m.Attachment,
            'plans':m.PaymentPlan}
TIME_KEYS = {'archived_at','deleted_at','purged_at','created_at','updated_at'}


def field_for(model,key):
    if model is None:return None
    if model is m.Handoff and key=='target':key='target_organization'
    try:return model._meta.get_field(key)
    except FieldDoesNotExist:return None


def child_metadata(model,key):
    field=field_for(model,key)
    label=LABELS.get(key) or str(getattr(field,'verbose_name',key))
    row_spec=ROW_SPECS.get(model)
    if row_spec and key==row_spec.parent_field:label=row_spec.parent_label
    if key=='order' and field is not None and str(field.verbose_name)!='order':label=str(field.verbose_name)
    if model is m.Attachment and key=='archived_at':label='归档时间'
    if model is not None and field is not None and key=='version' and issubclass(model,m.Record):label='并发校验版本'
    child_model=WRAPPERS.get(key)
    if key=='line':
        spec=LINE_SPECS.get(model)
        child_model=spec.model if spec else None
        if spec:label=spec.snapshot_label
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
        yield {'path':path,'labels':labels,'key':' / '.join(labels) or '内容','raw':value,'field':field,
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
    if value is None:
        key=row.get('path',('',))[-1:]
        return {'archived_at':'尚未归档','deleted_at':'尚未删除','purged_at':'尚未清理'}.get(key[0] if key else '', '空值（null）')
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
    if isinstance(field,models.FileField) and field.model is m.Attachment:
        return '私有文件（存储路径见原始 JSON）'
    path=row.get('path',())
    if path and path[-1]=='object_type' and isinstance(value,str):return audit_type_title(value)
    if isinstance(field,models.DateTimeField) or path and path[-1] in TIME_KEYS:
        formatted=local_timestamp(value)
        if formatted is not None:return formatted
    if field is not None:
        for stored,label in getattr(field,'flatchoices',()):
            if str(stored)==str(value):return str(label)
        if isinstance(field,models.BooleanField) and value in ('True','False'):
            return '是' if value=='True' else '否'
    return str(value)


def compare_payloads(before,after,model,names):
    differences=[];recorded=[]

    def record(stage,value,model,path,labels,field):
        recorded.extend(dict(row,stage=stage,value=display(row,names))
                        for row in flatten(value,model,path,labels,field,summary_lists=True))

    def compare(a,b,model,path=(),labels=(),field=None,other_path=None):
        if other_path is None:other_path=path
        if same_value(a,b):return
        if isinstance(a,dict) and isinstance(b,dict):
            for key in dict.fromkeys([*a,*b]):
                child_field,label,child_model=child_metadata(model,str(key))
                child_path=path+(str(key),);child_labels=labels+(label,)
                if key in a and key in b:
                    compare(a[key],b[key],child_model,child_path,child_labels,child_field,other_path+(str(key),))
                elif key in a:record('变更前',a[key],child_model,child_path,child_labels,child_field)
                else:record('变更后',b[key],child_model,other_path+(str(key),),child_labels,child_field)
        elif isinstance(a,list) and isinstance(b,list):
            match=list_identity(a,b)
            if match and set(match[1])==set(match[2]):
                key,left,right=match;indexed={value:(index,item) for index,(value,item) in enumerate(zip(right,b))}
                for index,(value,item) in enumerate(zip(left,a)):
                    new_index,new_item=indexed[value]
                    compare(item,new_item,model,path+(index,),labels+(value if key=='code' else f'ID: {value}',),field,other_path+(new_index,))
                if left!=right:
                    differences.append({'key':(' / '.join(labels) or '内容')+' / 排列顺序',
                                        'before':' → '.join(left),'after':' → '.join(right)})
                return
            note='含新增/移除条目，不推断对应关系。' if match else '缺少唯一标识或标识重复，不推断对应关系。'
            differences.append({'key':' / '.join(labels) or '内容',
                'before':f'{len(a)} 条（内容见完整快照）' if a else '0 条（无条目）',
                'after':(f'{len(b)} 条（条数不变，内容或顺序有变化；见完整快照）' if len(a)==len(b)
                         else f'{len(b)} 条（内容见完整快照）' if b else '0 条（已清空）'),'note':note})
        elif isinstance(a,(dict,list)) or isinstance(b,(dict,list)):
            record('变更前',a,model,path,labels,field)
            record('变更后',b,model,other_path,labels,field)
        else:
            if field is not None and (getattr(field,'primary_key',False) or isinstance(field,models.ForeignKey)):
                left=identity(a,'id');right=identity(b,'id')
                if left is not None and left==right:return
            old={'raw':a,'field':field,'path':path};new={'raw':b,'field':field,'path':other_path}
            technical=field is not None and field.name=='version' and issubclass(field.model,m.Record)
            differences.append({'key':' / '.join(labels),'before':display(old,names),'after':display(new,names),
                                'technical':technical,'before_path':path,'after_path':other_path,'before_raw':a,'after_raw':b})

    compare(before,after,model)
    return differences,recorded


def line_operation(event,values,model,names):
    actions={'add_line':('变更后','新增'),'delete_line':('变更前','移除'),
             'add_change_line':('变更后','新增'),'remove_change_line':('变更前','移除')}
    action=actions.get(event.action);spec=LINE_SPECS.get(model)
    if action is None or spec is None:return None
    if event.action in ('add_line','delete_line') and not spec.generic_editor:return None
    if event.action in ('add_change_line','remove_change_line') and model is not m.ChangeRequest:return None
    stage,verb=action;index=1 if stage=='变更后' else 0
    value,other=values[index],values[1-index]
    if not isinstance(value,dict) or not isinstance(other,dict) or 'line' in other:return None
    row=value.get('line')
    if not isinstance(row,dict):return None
    row_id=identity(row.get('id'),'id');parent_id=identity(row.get(spec.parent_field),'id')
    if row_id is None or parent_id is None or parent_id!=identity(event.object_id,'id'):return None
    name=row.get('name')
    if not isinstance(name,str) or not name.strip():
        name=''
        if identity(row.get('material'),'id') is not None:
            name=display({'raw':row['material'],'field':field_for(spec.model,'material'),'path':('line','material')},names)
    noun='申请明细' if model is m.ChangeRequest else '明细'
    description=(name+' · ' if name else '')+f'明细 ID: {row_id}'
    return {'title':f'{verb} 1 条{noun}','verb':verb,'noun':noun,'stage':stage,'description':description}


def audit_context(user,event,organization=None):
    model,_=audit_subject(event)
    stages=[];values=[];normalization_notes=[]
    for title,value in [('变更前',event.before),('变更后',event.after)]:
        value,note=normalize_stage(event,value)
        if note:normalization_notes.append(f'{title}：{note}')
        if not isinstance(value,(dict,list)):value={'内容':value}
        values.append(value)
        stages.append((title,list(flatten(value,model))))
    names=reference_names(user,event,organization,[row for _,rows in stages for row in rows])
    snapshots=[{'title':title,'rows':[dict(row,value=display(row,names)) for row in rows]} for title,rows in stages]
    all_differences,recorded=compare_payloads(*values,model,names)
    differences=[r for r in all_differences if not r.get('technical')]
    technical=[r for r in all_differences if r.get('technical')]
    operation=line_operation(event,values,model,names)
    is_write=not values[0]
    write_rows=[dict(row,value=display(row,names)) for row in flatten(values[1],model,summary_lists=True)] if is_write else []

    def covers(rows,full):
        left={r['path']:r['raw'] for r in rows};right={r['path']:r['raw'] for r in full}
        return bool(left) and same_value(left,right)

    covered=[{r['path']:r['raw'] for r in recorded if r['stage']==title} for title in ('变更前','变更后')]
    if is_write:covered[1].update({r['path']:r['raw'] for r in write_rows})
    for row in all_differences:
        if 'before_path' in row:
            covered[0][row['before_path']]=row['before_raw'];covered[1][row['after_path']]=row['after_raw']
    remaining=[]
    for index,payload in enumerate(snapshots):
        remaining.append([row for row in payload['rows'] if row['path'] not in covered[index]
                          or not same_value(row['raw'],covered[index][row['path']])])
    logical=[canonical_rows(values[i],rows,str(i)) for i,rows in enumerate(remaining)]
    common={path for path in logical[0].keys() & logical[1].keys()
            if same_value(logical[0][path]['raw'],logical[1][path]['raw'])}
    shared=[dict(row,key=row['canonical_key']) for path,row in logical[0].items() if path in common]
    # A known producer records the parent only as before-context. Absence of an
    # after value does not prove it unchanged; never generalize to subject state.
    parent_context=[row for row in recorded if row['stage']=='变更前' and row['path'][:1]==('handoff',)] \
        if event.object_type=='handoffs' and event.action=='stop_sharing_attachment' and \
           isinstance(values[0].get('handoff') if isinstance(values[0],dict) else None,dict) and \
           isinstance(values[1],dict) and 'handoff' not in values[1] else []
    context_paths={row['path'] for row in parent_context}
    context_payloads=[{'title':'变更前','rows':parent_context}] if parent_context else []
    recorded_payloads=[]
    for index,payload in enumerate(snapshots):
        rows=[r for r in recorded if r['stage']==payload['title'] and not (r['stage']=='变更前' and r['path'] in context_paths)]
        complete=covers(rows,payload['rows'])
        payload['render_rows']=[row for path,row in logical[index].items() if path not in common]
        payload['partial']=len(payload['render_rows'])!=len(payload['rows'])
        payload['show']=bool(payload['render_rows'])
        payload['context_note']='含操作时父单据上下文（交接快照），仅前侧有记录；未记录的后值不作推断。' \
            if payload['title']=='变更前' and parent_context else ''
        if rows:recorded_payloads.append({'title':payload['title'],'rows':rows,'complete':complete})
    counted=[r for r in recorded if not (r['stage']=='变更前' and r['path'] in context_paths)
             and not (operation and r['stage']==operation['stage'] and r['path'][:1]==('line',))]
    after_count=sum(r['stage']=='变更后' for r in counted)
    before_count=sum(r['stage']=='变更前' for r in counted)
    heading='本次变更' if operation else '本次写入' if is_write else '本次变更' if differences else '本次操作记录'
    if is_write and not operation:summary=f'{len(write_rows)} 个记录字段'
    else:
        parts=[operation['title']] if operation else []
        if differences:parts.append(f'{len(differences)} 项业务变化')
        if technical:parts.append(f'{len(technical)} 项校验信息更新')
        if after_count:parts.append(f'{after_count} 个结果字段')
        if before_count:parts.append(f'{before_count} 个操作前字段')
        summary=' · '.join(parts) or '未记录可比较字段变化'
    missing_line=event.action in ('add_line','add_change_line') and not (
        isinstance(event.after,dict) and isinstance(event.after.get('line'),dict) and event.after['line'])
    return {'audit_payloads':snapshots,'audit_differences':differences,
            'audit_technical_differences':technical,'audit_shared_rows':shared,
            'audit_context_payloads':context_payloads,
            'audit_line_operation':operation,'audit_normalization_notes':normalization_notes,
            'audit_evidence_warning':'本条增行事件未记录被添加的明细，无法从本条审计还原当时的物料、数量或单价。' if missing_line else '',
            'audit_is_write':is_write,'audit_write_rows':write_rows,'audit_heading':heading,'audit_summary':summary,
            'audit_recorded_fields':recorded,
            'audit_recorded_payloads':list(reversed(recorded_payloads)),
            'audit_raw':json.dumps({'before':event.before,'after':event.after},ensure_ascii=False,indent=2)}
