"""Audit subject identity, independent of permission and navigation modules."""
from decimal import Decimal
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
