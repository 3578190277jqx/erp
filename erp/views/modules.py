import csv
from decimal import Decimal
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction, IntegrityError, OperationalError
from django.db.models.deletion import ProtectedError
from django.db.models import Q, Sum, F, OuterRef, Subquery, DecimalField, Value
from django.db.models.functions import Coalesce
from django.http import Http404, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.views.decorators.http import require_POST
from erp import models as m
from erp.forms import document_form, line_form, StockAdjustmentForm, PaymentForm
from erp.permissions import can, scoped
from erp.registry import MODULES, editable
from erp.services import transition, add_line, adjust_stock, record_payment, available_actions
from erp.services.common import lock, snapshot, save_version, audit

BUSINESS_ERRORS = (ValidationError, IntegrityError, OperationalError)


def active_org(request):
    return request.session.get('organization_id')


def module_for(key):
    try:
        return MODULES[key]
    except KeyError:
        raise Http404


def authorize(request, module, action='view'):
    if not can(request.user, module.model.module, action, active_org(request)):
        raise PermissionDenied('当前角色没有此模块的访问权限。')


def error_message(exc):
    if isinstance(exc,ProtectedError):return '该记录已被业务单据或生产 BOM 引用，不能直接删除，请先处理关联业务。'
    if isinstance(exc, ValidationError):
        return '；'.join(exc.messages)
    if isinstance(exc, OperationalError):
        return '当前有其他事务正在更新数据，请刷新后重试。'
    return '记录重复或关联数据已发生变化，请检查后重试。'


def record_for(request, module, pk):
    return get_object_or_404(scoped(request.user, module.model, active_org(request)), pk=pk)


def filtered(request, module):
    qs = scoped(request.user, module.model, active_org(request))
    if module.key=='customers' and request.GET.get('include_internal')!='1':qs=qs.filter(is_internal=False)
    for field in module.model._meta.fields:
        if isinstance(field, m.models.ForeignKey):
            qs = qs.select_related(field.name)
    query = request.GET.get('q', '').strip()[:150]
    if query:
        predicate = Q(code__icontains=query)
        for name in ('name', 'title', 'phone', 'supplier', 'reference', 'work_center'):
            if any(f.name == name for f in module.model._meta.fields):
                predicate |= Q(**{f'{name}__icontains': query})
        qs = qs.filter(predicate)
    if 'status' in [f.name for f in module.model._meta.fields] and request.GET.get('status'):
        qs = qs.filter(status=request.GET['status'])
    if request.GET.get('overdue') == '1' and 'due_date' in [f.name for f in module.model._meta.fields]:
        from django.utils import timezone
        qs = qs.filter(due_date__lt=timezone.localdate()).exclude(status__in=['completed', 'cancelled', 'closed', 'received', 'delivered', 'lost'])
    from datetime import date
    for param,lookup in [('date_from','created_at__date__gte'),('date_to','created_at__date__lte')]:
        value=request.GET.get(param,'')
        if value:
            try:qs=qs.filter(**{lookup:date.fromisoformat(value)})
            except ValueError:pass
    if request.GET.get('mine')=='1':qs=qs.filter(owner=request.user)
    sorting=request.GET.get('sort','-created_at')
    columns=set(module.columns)&{f.name for f in module.model._meta.fields}
    allowed={'created_at','-created_at','code','-code'}|columns|{f'-{field}' for field in columns}
    if sorting not in allowed:sorting='-created_at'
    return qs.order_by(sorting,'-pk')


def display_value(obj, field):
    value = getattr(obj, field, None)
    if value is None or value == '':
        return '—'
    if hasattr(obj, f'get_{field}_display'):
        return getattr(obj, f'get_{field}_display')()
    if field in ('owner', 'requested_by'):
        return value.first_name or value.username
    if field == 'action':
        from erp.templatetags.ui import action_name
        return action_name(value)
    if isinstance(value, Decimal):
        return f'¥{value:,.2f}' if field in ('total', 'amount', 'unit_price', 'unit_cost', 'proposed_total') else f'{value:,.3f}'.rstrip('0').rstrip('.')
    if hasattr(value, 'strftime'):
        return value.strftime('%Y-%m-%d %H:%M') if field.endswith('_at') else value.strftime('%Y-%m-%d')
    return str(value)


def rows_for(objects, columns):
    return [{'object': obj, 'cells': [{'name': field, 'value': display_value(obj, field)} for field in columns]} for obj in objects]


def label(model, field):
    if field=='available':return '可用量'
    return model._meta.get_field(field).verbose_name


def finance_summary(request):
    orders = scoped(request.user, m.Order, active_org(request)).exclude(status__in=['draft', 'cancelled'])
    purchases = scoped(request.user, m.Purchase, active_org(request)).filter(status__in=['ordered', 'partial', 'received'])
    payments = scoped(request.user, m.Payment, active_org(request))
    receivables = []
    payables = []
    totals=[]
    for qs, key, target in [(orders, 'order', receivables), (purchases, 'purchase', payables)]:
        payment_total=payments.filter(**{key:OuterRef('pk')}).order_by().values(key).annotate(s=Sum('amount')).values('s')
        currency=DecimalField(max_digits=14,decimal_places=2)
        qs=qs.annotate(paid_value=Coalesce(Subquery(payment_total,output_field=currency),Value(Decimal(0)),output_field=currency))
        qs=qs.annotate(balance_value=F('total')-F('paid_value'))
        totals.append(qs.aggregate(s=Sum('balance_value'))['s'] or Decimal(0))
        for document in qs.order_by('due_date','pk')[:50]:
            target.append({'object': document, 'total': document.total, 'paid': document.paid_value, 'balance': document.balance_value,
                           'module': 'orders' if key == 'order' else 'purchases'})
    return {'receivables': receivables, 'payables': payables,
            'receivable_total': totals[0], 'payable_total': totals[1]}


@login_required
def list_records(request, key):
    module = module_for(key)
    authorize(request, module)
    qs = filtered(request, module)
    page_size=request.GET.get('page_size','15')
    page_size=int(page_size) if page_size in ('10','15','30','50') else 15
    page = Paginator(qs, page_size).get_page(request.GET.get('page'))
    statuses = module.model._meta.get_field('status').choices if any(f.name == 'status' for f in module.model._meta.fields) else []
    context = {'module': module, 'page_obj': page, 'rows': rows_for(page, module.columns),
               'headers': [label(module.model, f) for f in module.columns], 'statuses': statuses,
               'can_create': module.creatable and can(request.user, module.model.module, 'create', active_org(request)),
               'can_adjust': key == 'inventory' and can(request.user, 'inventory', 'adjust', active_org(request)),
               'can_pay': key == 'finance' and can(request.user, 'finance', 'create', active_org(request)),
               'batch_actions': [(a, name) for a, name in [('approve', '批量批准'), ('assign', '批量派工')]
                                 if can(request.user, module.model.module, a, active_org(request)) and statuses],
               'query_string': request.GET.urlencode(), 'q': request.GET.get('q', ''), 'selected_status': request.GET.get('status', ''),
               'can_import':key in ('customers','materials') and can(request.user,key,'create',active_org(request)),
               'page_size':str(page_size),'page_sizes':['10','15','30','50'],'sort':request.GET.get('sort','-created_at')}
    params=request.GET.copy();params.pop('page',None);context['pagination_query']=params.urlencode()
    if key == 'finance':
        context.update(finance_summary(request))
    if key == 'inventory':
        context['warehouses'] = scoped(request.user, m.Warehouse, active_org(request))
        purchases=scoped(request.user,m.Purchase,active_org(request)).filter(status__in=['ordered','partial'])
        context['in_transit']=m.PurchaseLine.objects.filter(purchase__in=purchases,received_qty__lt=F('quantity')).annotate(
            remaining=F('quantity')-F('received_qty')).select_related('purchase','material','purchase__warehouse').order_by('purchase__due_date','pk')[:30]
    return render(request, 'erp/list.html', context)


@login_required
def detail(request, key, pk):
    module = module_for(key)
    obj = record_for(request, module, pk)
    fields = ['organization', 'owner', *module.columns, *module.fields]
    if isinstance(obj, m.ChangeRequest):
        fields.append('requested_by')
    if isinstance(obj, m.AuditEvent):
        fields.extend(['before', 'after'])
    fields = list(dict.fromkeys(fields))
    info = [{'label': label(module.model, name), 'value': display_value(obj, name)} for name in fields]
    context = {'module': module, 'obj': obj, 'info': info,
               'detail_title': getattr(obj, 'title', None) or getattr(obj, 'name', None) or module.singular,
               'can_edit': editable(obj) and can(request.user, module.model.module, 'edit', obj.organization_id)
                           and scoped(request.user, module.model, action='edit').filter(pk=obj.pk).exists(),
               'actions': [(a, text) for a, text in available_actions(obj) if scoped(request.user, module.model, action=a).filter(pk=obj.pk).exists()] if hasattr(obj, 'status') else [],
               'events': m.AuditEvent.objects.filter(organization=obj.organization, code=obj.code)[:12]}
    if isinstance(obj, (m.Quote, m.Purchase, m.Breakdown)):
        cols = {'quotes': ('name', 'room', 'specification', 'pricing_mode', 'billable_quantity', 'unit', 'unit_price', 'subtotal'),
                'purchases': ('material', 'quantity', 'received_qty', 'unit_price', 'subtotal'),
                'breakdowns': ('order_line','material', 'quantity', 'waste', 'required')}[key]
        names = {'subtotal': '小计', 'required': '含损耗总需求', 'billable_quantity':'计价数量'}
        model = obj.lines.model
        context.update({'line_rows': rows_for(obj.lines.all(), cols),
                        'line_headers': [names.get(c) or label(model, c) for c in cols],
                        'line_form': line_form(request.user, obj),
                        'can_lines': obj.status in ('draft', 'rejected') and scoped(request.user, module.model, action='edit').filter(pk=obj.pk).exists()})
    related = []
    for other in MODULES.values():
        if other.key in ('audit', 'movements', 'inventory', 'warehouses'):
            continue
        for field in other.model._meta.fields:
            if isinstance(field, m.models.ForeignKey) and field.related_model == type(obj):
                related.extend({'module': other, 'obj': child} for child in scoped(request.user, other.model).filter(**{field.name: obj})[:8])
    context['related'] = related
    context['new_related'] = []
    for target, parent_field in [('measurements', 'customer'), ('designs', 'customer'), ('quotes', 'customer'),
                                 ('followups','customer'), ('breakdowns', 'order'), ('deliveries', 'order'), ('installations', 'order'), ('aftersales', 'order'), ('changes', 'order')]:
        target_module = MODULES[target]
        parent_model = target_module.model._meta.get_field(parent_field).related_model
        if isinstance(obj, parent_model) and can(request.user, target_module.model.module, 'create', obj.organization_id):
            if isinstance(obj,m.Order) and (obj.source_handoff_id and target=='changes' or target=='breakdowns' and m.Handoff.objects.filter(order=obj,status__in=['sent','accepted','completed']).exists()):
                continue
            context['new_related'].append({'module': target_module, 'field': parent_field})
    if isinstance(obj, (m.Order, m.Purchase)):
        if can(request.user, 'finance', 'view', obj.organization_id):
            paid = scoped(request.user, m.Payment).filter(**{'order' if isinstance(obj, m.Order) else 'purchase': obj}).aggregate(s=Sum('amount'))['s'] or 0
            context.update({'paid': paid, 'balance': obj.total - paid})
    from .features import feature_context
    context.update(feature_context(request,obj))
    return render(request, 'erp/detail.html', context)


def validate_creation(obj):
    if isinstance(obj, m.Breakdown) and obj.order.status != 'confirmed':
        raise ValidationError('只有已确认订单可以拆单。')
    if isinstance(obj,m.Breakdown) and m.Handoff.objects.filter(order=obj.order,status__in=['sent','accepted','completed']).exists():
        raise ValidationError('此订单已交接，请由工厂在本地生产订单上拆单。')
    if isinstance(obj, m.ChangeRequest):
        if obj.order.status not in ('confirmed', 'producing', 'ready', 'shipping', 'installing') or not obj.note.strip():
            raise ValidationError('请选择履约中的订单，并填写变更原因。')
        if obj.order.source_handoff_id:
            raise ValidationError('工厂生产订单的交接快照不可通过本地变更修订。')
        obj.order_version = obj.order.version


@login_required
def edit_record(request, key, pk=None):
    module = module_for(key)
    if key == 'finance' and pk is None:
        return payment_create(request)
    authorize(request, module, 'edit' if pk else 'create')
    if not pk and not module.creatable:
        raise PermissionDenied('此模块通过业务流程生成。')
    obj = record_for(request, module, pk) if pk else None
    if obj and not editable(obj):
        raise PermissionDenied('此单据已冻结，请使用变更或状态操作。')
    form = document_form(module, request.user, request.POST if request.method == 'POST' else None, obj, active_org(request) or request.GET.get('organization'))
    if request.method == 'GET':
        for name in ('customer', 'order'):
            if name in form.fields and request.GET.get(name):
                selected = form.fields[name].queryset.filter(pk=request.GET[name]).first()
                if selected:
                    form.initial[name] = selected.pk
    if request.method == 'POST' and form.is_valid():
        try:
            with transaction.atomic():
                if obj:
                    # ModelForm mutates instance before saving, so lock a fresh DB copy for before-values.
                    if isinstance(obj,m.Production):
                        # Use the same parent -> job lock order as reserve/issue.
                        production_order=m.Production.objects.get(pk=obj.pk).breakdown.order_id
                        m.Order.objects.select_for_update().get(pk=production_order)
                    fresh = lock(request.user, obj, 'edit', form.cleaned_data['version'])
                    before = snapshot(fresh)
                    if not editable(fresh):
                        raise ValidationError('单据已冻结，请刷新。')
                    candidate = form.save(commit=False)
                    if isinstance(candidate,m.Production) and candidate.warehouse_id!=fresh.warehouse_id and fresh.reservations.filter(status='active').exists():
                        raise ValidationError('此工单已有物料预留，请先释放预留后再更换领料仓库。')
                    if isinstance(candidate,m.Purchase) and candidate.supplier_record_id:candidate.supplier=candidate.supplier_record.name
                    if isinstance(candidate, m.Material) and candidate.unit != fresh.unit and m.StockMovement.objects.filter(material=candidate).exists():
                        raise ValidationError('已有库存流水的物料不能改变计量单位。')
                    if isinstance(candidate, m.Quote):
                        from erp.services.pricing import quote_amounts
                        candidate.total = quote_amounts(candidate)['total']
                    save_version(candidate, fresh.version)
                    audit(request.user, candidate, 'edit', before)
                else:
                    candidate = form.save(commit=False)
                    if isinstance(candidate,m.Purchase) and candidate.supplier_record_id:candidate.supplier=candidate.supplier_record.name
                    validate_creation(candidate)
                    if isinstance(candidate, m.ChangeRequest):
                        candidate.requested_by = request.user
                    candidate.full_clean(exclude=['code'])
                    candidate.save()
                    audit(request.user, candidate, 'create')
                    if isinstance(candidate,m.FollowUp) and candidate.next_date:
                        from erp.services.common import system_update
                        system_update(request.user,candidate.customer,'followup',due_date=candidate.next_date)
            messages.success(request, '已保存，所有更改均已记录。')
            return redirect('detail', key=key, pk=candidate.pk)
        except BUSINESS_ERRORS as exc:
            form.add_error(None, error_message(exc))
    return render(request, 'erp/form.html', {'module': module, 'obj': obj, 'form': form,
                                            'title': f'编辑{module.singular}' if pk else f'新建{module.singular}'})


@login_required
@require_POST
def act(request, key, pk):
    module = module_for(key)
    obj = record_for(request, module, pk)
    try:
        transition(request.user, obj, request.POST.get('action'), request.POST.get('version'), request.POST.get('note', ''))
        messages.success(request, '操作完成，业务状态已同步。')
    except BUSINESS_ERRORS as exc:
        messages.error(request, error_message(exc))
    return redirect('detail', key=key, pk=pk)


@login_required
@require_POST
def lines(request, key, pk):
    module = module_for(key)
    obj = record_for(request, module, pk)
    if not isinstance(obj, (m.Quote, m.Purchase, m.Breakdown)):
        raise Http404
    try:
        if request.POST.get('delete_id'):
            add_line(request.user, obj, {}, request.POST.get('version'), request.POST['delete_id'])
        else:
            form = line_form(request.user, obj, request.POST)
            if not form.is_valid():
                raise ValidationError([f'{form.fields[k].label if k in form.fields else "明细"}: {error}' for k, errors in form.errors.items() for error in errors])
            values = dict(form.cleaned_data)
            version = values.pop('version')
            add_line(request.user, obj, values, version)
        messages.success(request, '明细已更新，总额由服务器重新计算。')
    except BUSINESS_ERRORS as exc:
        messages.error(request, error_message(exc))
    return redirect('detail', key=key, pk=pk)


@login_required
@require_POST
def batch(request, key):
    module = module_for(key)
    action = request.POST.get('action')
    if action not in ('approve', 'assign'):
        raise PermissionDenied
    authorize(request, module, action)
    ids, versions = request.POST.getlist('ids'), request.POST.getlist('versions')
    try:
        if not ids or len(ids) != len(versions) or len(ids) > 100 or len(set(ids)) != len(ids):
            raise ValidationError('请选择 1—100 条有效记录。')
        with transaction.atomic():
            for pk, version in zip(ids, versions):
                obj = record_for(request, module, pk)
                transition(request.user, obj, action, version, request.POST.get('note', ''))
        messages.success(request, f'已处理 {len(ids)} 条记录。')
    except BUSINESS_ERRORS as exc:
        messages.error(request, f'本次批量操作全部撤回：{error_message(exc)}')
    return redirect('list', key=key)


@login_required
def export(request, key):
    module = module_for(key)
    authorize(request, module, 'export')
    objects = list(filtered(request, module)[:10000])
    response = HttpResponse(content_type='text/csv; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{key}.csv"'
    response.write('\ufeff')
    writer = csv.writer(response)
    writer.writerow([label(module.model, f) for f in module.columns])
    for obj in objects:
        values = [display_value(obj, field) for field in module.columns]
        writer.writerow(["'" + v if v.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])
    for org_id in set(obj.organization_id for obj in objects):
        m.AuditEvent.objects.create(organization_id=org_id,owner=request.user,object_type='system',object_id=0,code='CSV-EXPORT',
                                   action='export',after={'module':key,'rows':sum(1 for obj in objects if obj.organization_id==org_id)})
    return response


@login_required
def inventory_adjust(request):
    if not can(request.user, 'inventory', 'adjust', active_org(request)):
        raise PermissionDenied
    form = StockAdjustmentForm(request.user, active_org(request), data=request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            data = form.cleaned_data
            adjust_stock(request.user, data['warehouse'], data['material'], data['quantity'], data['reason'])
            messages.success(request, '库存已调整，凭证已写入流水。')
            return redirect('list', key='inventory')
        except BUSINESS_ERRORS as exc:
            form.add_error(None, error_message(exc))
    return render(request, 'erp/form.html', {'title': '库存调整', 'form': form, 'module': MODULES['inventory']})


@login_required
def payment_create(request):
    if not can(request.user, 'finance', 'create', active_org(request)):
        raise PermissionDenied
    form = PaymentForm(request.user, active_org(request), data=request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        try:
            data = form.cleaned_data
            record_payment(request.user, data['order'] if data['direction'] == 'in' else data['purchase'],
                           data['amount'], data['reference'], data['paid_date'], data['method'], data['note'],plan=data.get('plan'))
            messages.success(request, '收付款已登记，余额已同步。')
            return redirect('list', key='finance')
        except BUSINESS_ERRORS as exc:
            form.add_error(None, error_message(exc))
    return render(request, 'erp/form.html', {'title': '登记收付款', 'form': form, 'module': MODULES['finance']})
