"""Audit subject identity, independent of permission and navigation modules."""
from decimal import Decimal
import re
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from . import models as m
from .audit_values import identity
from .registry import MODULES

PLAN_TYPE = 'payment_plans'
PLAN_TITLE = '分期收款计划'
ATTACHMENT_ACTIONS = {
    'delete_attachment':('deleted_at','已记录删除附件'),
    'stop_sharing_attachment':('archived_at','已记录停止共享'),
}


def audit_type_for(obj):
    return PLAN_TYPE if isinstance(obj,m.PaymentPlan) else obj.module


def _legacy_plan(event):
    # No live-object lookup: Payment and PaymentPlan can share both IDs and PAY prefixes.
    if event.object_type!='finance' or event.action not in ('revise_payment_plan','reschedule_payment_plan'):
        return False
    target=identity(event.object_id,'id')
    if target is None or identity(event.code,'code') is None:return False
    for payload in (event.before,event.after):
        if not isinstance(payload,dict) or not {'id','code','order','sequence','gate','amount'}<=payload.keys():
            return False
        if {'direction','purchase','paid_date'} & payload.keys():return False
        if identity(payload['id'],'id')!=target or payload['code']!=event.code:
            return False
        if identity(payload['sequence'],'id') is None or identity(payload['order'],'id') is None:
            return False
        if payload['gate'] not in ('start','ship','complete'):return False
        value=payload['amount']
        if isinstance(value,bool) or not isinstance(value,(str,int,float,Decimal)):return False
        try:m.PaymentPlan._meta.get_field('amount').clean(str(value),None)
        except ValidationError:return False
    return True


def audit_subject(event):
    if event.object_type==PLAN_TYPE or _legacy_plan(event):
        return m.PaymentPlan,PLAN_TITLE
    module=MODULES.get(event.object_type)
    return (module.model,module.title) if module else (None,event.object_type)


def audit_type_title(key):
    if key==PLAN_TYPE:return PLAN_TITLE
    module=MODULES.get(key)
    return module.title if module else key


def _attachment_fingerprint(event,payload):
    """Validate recorded identity only; never consult today's attachment object."""
    fingerprint={'id','code','organization','object_type','object_id','file','original_name','sha256','kind','size'}
    if event.action not in ATTACHMENT_ACTIONS or not isinstance(payload,dict) or not fingerprint<=payload.keys():
        return False
    if event.action=='stop_sharing_attachment' and event.object_type!='handoffs':return False
    if (event.object_type not in MODULES or payload['object_type']!=event.object_type or
        identity(payload['object_id'],'id') is None or identity(payload['object_id'],'id')!=identity(event.object_id,'id') or
        identity(payload['organization'],'id') is None or identity(payload['organization'],'id')!=identity(event.organization_id,'id') or
        identity(payload['id'],'id') is None or identity(payload['code'],'code') is None):
        return False
    if not all(isinstance(payload[key],str) and payload[key].strip() for key in ('file','original_name','sha256')):
        return False
    if not re.fullmatch(r'[0-9a-fA-F]{64}',payload['sha256']):return False
    if not isinstance(payload['kind'],str) or payload['kind'] not in {
        choice[0] for choice in m.Attachment._meta.get_field('kind').flatchoices}:return False
    size=payload['size']
    if isinstance(size,bool) or not isinstance(size,(str,int)) or not str(size).isascii() or not str(size).isdigit():
        return False
    return True


def normalize_stage(event,payload):
    """Interpret a recorded flat stage without changing JSON or guessing its age."""
    if not _attachment_fingerprint(event,payload):return payload,''
    return {'attachment_state':payload},'扁平附件载荷按附件字段解释，原始 JSON 保留原结构。'


def normalize_attachment_transition(event,before,after):
    """Align explicit attachment aliases; missing historical state stays missing."""
    unchanged=(before,after,None)
    if event.action not in ATTACHMENT_ACTIONS or not isinstance(before,dict) or not isinstance(after,dict):return unchanged
    candidates=[key for key in ('attachment_state','attachment') if key in before]
    if len(candidates)!=1:return unchanged
    key=candidates[0];state=before[key]
    if not _attachment_fingerprint(event,state) or after.get('attachment')!=state['code']:return unchanged
    if not _attachment_parent_matches(event,before,after):return unchanged
    if 'attachment_state' in after:
        if not isinstance(after['attachment_state'],dict):return unchanged
        patch=dict(after['attachment_state'])
    else:
        # The unwrapped historical patch needs both recorded code and checksum.
        if after.get('sha256')!=state['sha256']:return unchanged
        patch={}
    for field in ('id','organization','object_id'):
        if field in patch and identity(patch[field],'id')!=identity(state[field],'id'):return unchanged
    for field in ('code','sha256','object_type'):
        if field in patch and patch[field]!=state[field]:return unchanged
    if 'sha256' in after and after['sha256']!=state['sha256']:return unchanged
    time_field,title=ATTACHMENT_ACTIONS[event.action]
    if time_field in after and time_field in patch and after[time_field]!=patch[time_field]:return unchanged
    timestamp=patch.get(time_field,after.get(time_field))
    try:
        parsed=parse_datetime(timestamp) if isinstance(timestamp,str) else None
    except (ValueError,OverflowError):return unchanged
    if parsed is None or timezone.is_naive(parsed):return unchanged
    patch['code']=after['attachment']
    if 'sha256' in after:patch['sha256']=after['sha256']
    patch[time_field]=timestamp
    left={k:v for k,v in before.items() if k!=key};left['attachment_state']=state
    right={k:v for k,v in after.items() if k not in ('attachment','sha256',time_field,'attachment_state')}
    right['attachment_state']=patch
    return left,right,{'title':title,'code':state['code'],'timestamp':timestamp}


def _attachment_parent_matches(event,*stages):
    for stage in stages:
        if not isinstance(stage,dict):return False
        contexts=[stage]+[stage[key] for key in ('handoff','handoffs') if key in stage]
        for payload in contexts:
            if not isinstance(payload,dict):return False
            for field,expected in (('id',event.object_id),('organization',event.organization_id),
                                   ('object_id',event.object_id)):
                if field in payload and identity(payload[field],'id')!=identity(expected,'id'):return False
            if 'code' in payload and payload['code']!=event.code:return False
            if 'object_type' in payload and payload['object_type']!=event.object_type:return False
    return True


def attachment_comparison_allowed(event,before,after,operation):
    """A rejected known pair stays separate evidence, not a state transition."""
    if event.action not in ATTACHMENT_ACTIONS or operation:return True
    if not _attachment_parent_matches(event,before,after):return False
    states=[]
    for stage in (before,after):
        keys=[key for key in ('attachment_state','attachment') if key in stage]
        if len(keys)!=1 or not _attachment_fingerprint(event,stage[keys[0]]):return False
        states.append(stage[keys[0]])
    left,right=states
    return all(identity(left[key],'id')==identity(right[key],'id') for key in ('id','organization','object_id')) and all(
        left[key]==right[key] for key in ('code','object_type','sha256'))
