import calendar
from datetime import date
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Sum, F
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
    for key in ['measurements', 'designs', 'quotes', 'breakdowns', 'production', 'purchases', 'deliveries', 'installations', 'aftersales', 'changes']:
        module = MODULES[key]
        qs = scoped_model(request, module.model).exclude(status__in=DONE)
        for obj in qs[:20]:
            item = {'obj': obj, 'module': module, 'due': getattr(obj, 'due_date', None)}
            tasks.append(item)
            if item['due'] and item['due'] < today:
                risk.append(item)
    tasks.sort(key=lambda t: (t['due'] or date.max, -t['obj'].pk))
    for order in open_orders.filter(due_date__lt=today)[:6]:
        risk.append({'obj': order, 'module': MODULES['orders'], 'due': order.due_date})
    months = []
    for offset in range(5, -1, -1):
        index = today.year * 12 + today.month - 1 - offset
        year, month0 = divmod(index, 12)
        amount = orders.filter(created_at__year=year, created_at__month=month0 + 1).exclude(status='draft').aggregate(s=Sum('total'))['s'] or 0
        months.append({'label': f'{month0 + 1}月', 'amount': amount})
    maximum = max([x['amount'] for x in months] + [1])
    points = ' '.join(f'{35 + i * 100},{180 - float(item["amount"] / maximum) * 130:.1f}' for i, item in enumerate(months))
    stages = []
    total_count = max(orders.count(), 1)
    for status, name in m.Order._meta.get_field('status').choices:
        count = orders.filter(status=status).count()
        if count:
            stages.append({'name': name, 'count': count, 'width': max(5, round(count / total_count * 100)), 'status': status})
    first_module = next((key for key in ('customers', 'measurements', 'designs', 'quotes', 'purchases', 'installations', 'aftersales')
                         if can(request.user, MODULES[key].model.module, 'create', active_org(request))), None)
    return render(request, 'erp/dashboard.html', {
        'today': today, 'order_value': order_value, 'open_count': open_orders.count(), 'task_count': len(tasks),
        'customer_count': customers.count(), 'tasks': tasks[:7], 'risks': risk[:5], 'risk_count': len(risk),
        'recent_orders': orders.select_related('customer', 'owner')[:5], 'months': months,
        'chart_points': points, 'stages': stages, 'new_module': MODULES.get(first_module),
        'completed_count': orders.filter(status='completed').count(),
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
            for status, label in module.model._meta.get_field('status').choices:
                counts.append({'label': label, 'count': qs.filter(status=status).count()})
        modules.append({'module': module, 'count': qs.count(), 'states': counts})
    context = {'report_modules': modules, 'low_stock': scoped_model(request, m.Stock).filter(quantity__lt=F('material__safety_stock')).select_related('material', 'warehouse')[:20]}
    if can(request.user, 'finance', 'view', active_org(request)):
        context.update(finance_summary(request))
    return render(request, 'erp/reports.html', context)


@login_required
def schedule(request):
    if not can(request.user, 'production', 'view', active_org(request)):
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    qs = scoped_model(request, m.Production).select_related('owner', 'breakdown__order').order_by('start_date', 'id')
    columns = []
    for status, label in m.Production._meta.get_field('status').choices:
        columns.append({'status': status, 'label': label, 'objects': list(qs.filter(status=status))})
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
