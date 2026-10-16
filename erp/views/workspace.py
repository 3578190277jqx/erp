import calendar
import json
from datetime import date
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum, F
from django.db.models.functions import TruncMonth
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.http import require_POST
from erp import models as m
from erp.permissions import scoped, can, accessible_organizations
from erp.registry import MODULES
from .modules import active_org, finance_summary

DONE = ['completed', 'cancelled', 'closed', 'received', 'delivered', 'approved', 'released', 'accepted', 'won', 'lost']


def scoped_model(request, model):
    return scoped(request.user, model, active_org(request))


@login_required
def dashboard(request):
    today = timezone.localdate()
    orders = scoped_model(request, m.Order).exclude(status='cancelled')
    quotes = scoped_model(request, m.Quote)
    customers = scoped_model(request, m.Customer)
    open_orders = orders.exclude(status__in=['completed', 'draft'])
    order_value = orders.exclude(status='draft').aggregate(s=Sum('total'))['s'] or Decimal('0')
    tasks = []
    risk = []
    task_count = risk_count = 0
    for key in ['measurements', 'designs', 'quotes', 'breakdowns', 'production', 'purchases', 'deliveries', 'installations', 'aftersales', 'changes']:
        module = MODULES[key]
        qs = scoped_model(request, module.model).exclude(status__in=DONE)
        task_count += qs.count()
        if any(field.name=='due_date' for field in module.model._meta.fields):
            risk_qs=qs.filter(due_date__lt=today)
            risk_count+=risk_qs.count()
            for obj in risk_qs.select_related('owner').order_by('due_date','pk')[:5]:
                risk.append({'obj':obj,'module':module,'due':obj.due_date})
            qs=qs.order_by('due_date','pk')
        for obj in qs.select_related('owner')[:7]:
            item = {'obj': obj, 'module': module, 'due': getattr(obj, 'due_date', None)}
            tasks.append(item)
    tasks.sort(key=lambda t: (t['due'] or date.max, -t['obj'].pk))
    risk_count += open_orders.filter(due_date__lt=today).count()
    for order in open_orders.filter(due_date__lt=today).select_related('owner').order_by('due_date','pk')[:5]:
        risk.append({'obj': order, 'module': MODULES['orders'], 'due': order.due_date})
    risk.sort(key=lambda t:(t['due'],t['obj'].pk))
    months = []
    month_totals={(row['month'].year,row['month'].month):row['amount'] for row in orders.exclude(status='draft').annotate(month=TruncMonth('created_at')).values('month').annotate(amount=Sum('total')).order_by()}
    for offset in range(5, -1, -1):
        index = today.year * 12 + today.month - 1 - offset
        year, month0 = divmod(index, 12)
        amount = month_totals.get((year,month0+1),0)
        months.append({'label': f'{month0 + 1}月', 'amount': amount})
    maximum = max([x['amount'] for x in months] + [1])
    points = ' '.join(f'{35 + i * 100},{180 - float(item["amount"] / maximum) * 130:.1f}' for i, item in enumerate(months))
    stages = []
    state_counts=dict(orders.values('status').annotate(n=Count('pk')).values_list('status','n'))
    total_count = max(sum(state_counts.values()), 1)
    for status, name in m.Order._meta.get_field('status').choices:
        count = state_counts.get(status,0)
        if count:
            stages.append({'name': name, 'count': count, 'width': max(5, round(count / total_count * 100)), 'status': status})
    first_module = next((key for key in ('customers', 'measurements', 'designs', 'quotes', 'purchases', 'installations', 'aftersales')
                         if can(request.user, MODULES[key].model.module, 'create', active_org(request))), None)
    return render(request, 'erp/dashboard.html', {
        'today': today, 'order_value': order_value, 'open_count': open_orders.count(), 'task_count': task_count,
        'customer_count': customers.count(), 'tasks': tasks[:7], 'risks': risk[:5], 'risk_count': risk_count,
        'recent_orders': orders.select_related('customer', 'owner')[:5], 'months': months,
        'chart_points': points, 'stages': stages, 'new_module': MODULES.get(first_module),
        'completed_count': state_counts.get('completed',0),
        'completion_percent': round(state_counts.get('completed',0) / total_count * 100),
        'quote_count': quotes.count(), 'can_see_orders': can(request.user, 'orders', 'view', active_org(request)),
    })


@login_required
def reports(request):
    modules = []
    for module in MODULES.values():
        if module.key in ('audit', 'warehouses', 'movements') or not can(request.user, module.model.module, 'view', active_org(request)):
            continue
        qs = scoped_model(request, module.model)
        fields = [f.name for f in module.model._meta.fields]
        counts = []
        if 'status' in fields:
            state_counts=dict(qs.values('status').annotate(n=Count('pk')).values_list('status','n'))
            for status, label in module.model._meta.get_field('status').choices:
                counts.append({'label': label, 'count': state_counts.get(status,0)})
            count=sum(state_counts.values())
        else:count=qs.count()
        modules.append({'module': module, 'count': count, 'states': counts})
    context = {'report_modules': modules, 'low_stock': scoped_model(request, m.Stock).filter(quantity__lt=F('reserved')+F('material__safety_stock')).select_related('material', 'warehouse')[:20]}
    if can(request.user, 'finance', 'view', active_org(request)):
        context.update(finance_summary(request))
    return render(request, 'erp/reports.html', context)


@login_required
def schedule(request):
    if not can(request.user, 'production', 'view', active_org(request)):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    qs = scoped_model(request, m.Production).select_related('owner', 'breakdown__order').order_by('start_date', 'id')
    from erp.services import available_actions
    from erp.services.workflows import TRANSITIONS
    columns = []
    for status, label in m.Production._meta.get_field('status').choices:
        objects = list(qs.filter(status=status))
        for obj in objects:
            actions = {TRANSITIONS[m.Production][action][1]: {'action': action, 'label': text}
                       for action, text in available_actions(obj)
                       if scoped(request.user, m.Production, action=action).filter(pk=obj.pk).exists()}
            obj.ui_transitions = json.dumps(actions, ensure_ascii=False)
            obj.ui_draggable = bool(actions)
        columns.append({'status': status, 'label': label, 'objects': objects})
    return render(request, 'erp/schedule.html', {'columns': columns, 'scheduled': qs.exclude(start_date=None)})


@login_required
def organizations(request):
    orgs = accessible_organizations(request.user)
    memberships = m.Membership.objects.filter(organization__in=orgs).select_related('user', 'organization')
    if not request.user.is_superuser:
        memberships = memberships.filter(user=request.user)
    return render(request, 'erp/organizations.html', {'organizations': orgs.select_related('parent'), 'memberships': memberships,
                                                     'roles': m.ROLES})


@login_required
@require_POST
def switch_organization(request):
    value = request.POST.get('organization_id')
    if not value:
        request.session.pop('organization_id', None)
    elif value.isdigit() and accessible_organizations(request.user).filter(pk=value).exists():
        request.session['organization_id'] = int(value)
    else:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    messages.success(request, '组织范围已切换。')
    return redirect('dashboard')
