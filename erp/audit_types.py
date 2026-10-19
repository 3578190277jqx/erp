"""Audit subject identity, independent of permission and navigation modules."""
from decimal import Decimal
import re
from django.core.exceptions import ValidationError
from . import models as m
from .audit_values import identity
from .registry import MODULES

PLAN_TYPE = 'payment_plans'
PLAN_TITLE = '分期收款计划'


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


def normalize_stage(event,payload):
    """Interpret a known legacy stage without modifying its stored JSON or subject."""
    fingerprint={'id','code','organization','object_type','object_id','file','original_name','sha256','kind','size'}
    if event.action!='delete_attachment' or not isinstance(payload,dict) or not fingerprint<=payload.keys():
        return payload,''
    if (event.object_type not in MODULES or payload['object_type']!=event.object_type or
        identity(payload['object_id'],'id') is None or identity(payload['object_id'],'id')!=identity(event.object_id,'id') or
        identity(payload['organization'],'id') is None or identity(payload['organization'],'id')!=identity(event.organization_id,'id') or
        identity(payload['id'],'id') is None or identity(payload['code'],'code') is None):
        return payload,''
    if not all(isinstance(payload[key],str) and payload[key].strip() for key in ('file','original_name','sha256')):
        return payload,''
    if not re.fullmatch(r'[0-9a-fA-F]{64}',payload['sha256']):return payload,''
    if payload['kind'] not in ('drawing','photo','contract'):return payload,''
    size=payload['size']
    if isinstance(size,bool) or not isinstance(size,(str,int)) or not str(size).isascii() or not str(size).isdigit():
        return payload,''
    return {'attachment_state':payload},'旧扁平附件载荷按附件字段解释，原始 JSON 保留原结构。'
