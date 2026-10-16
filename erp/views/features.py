"""Dedicated screens/actions for commerce and collaboration beyond generic CRUD."""
import csv
import io
from datetime import date
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.http import FileResponse,HttpResponse,Http404
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST
from erp import models as m
from erp.permissions import scoped,can,accessible_organizations
from erp.forms import style_fields
from erp.services import evidence,collaboration,materials,receivables,pricing,daily
from .modules import record_for,module_for,active_org,error_message,BUSINESS_ERRORS


def feature_context(request,obj):
    context={}
    if isinstance(obj,(m.Quote,m.Order)):
        if isinstance(obj,m.Quote):
            amounts=pricing.quote_amounts(obj)
        else:
            net=sum((line.line_total for line in obj.lines.all()),Decimal(0))
            cost=sum((pricing.rounded(line.billable_quantity*line.unit_cost) for line in obj.lines.all()),Decimal(0))
            amounts={'net':net,'tax':pricing.rounded(net*obj.tax_rate),'cost':cost,'margin':net-cost}
            context['snapshot_mismatch']=obj.total!=net+amounts['tax']
            context['order_lines']=obj.lines.all()
        context['amounts']=amounts
        context['show_costs']=can(request.user,'finance','view',obj.organization_id)
    if isinstance(obj,m.Order):
        context['plans']=receivables.plan_rows(obj)
        context['can_plan']=obj.kind=='sale' and obj.status=='confirmed' and not obj.payment_plans.exists() and (
            can(request.user,'finance','create',obj.organization_id) or can(request.user,'changes','approve',obj.organization_id))
        context['handoff']=m.Handoff.objects.filter(order=obj).first()
        context['can_handoff']=obj.kind=='sale' and obj.status=='confirmed' and scoped(request.user,m.Order,action='edit').filter(pk=obj.pk).exists() and not context['handoff']
        context['factories']=m.Organization.objects.filter(kind='factory').exclude(pk=obj.organization_id)
    if isinstance(obj,m.Production):
        context['can_reserve']=obj.status=='planned' and scoped(request.user,m.Production,action='reserve').filter(pk=obj.pk).exists()
        context['reservations']=obj.reservations.select_related('stock__material').all()
    if isinstance(obj,m.Purchase):
        context['can_receive']=obj.status in ('ordered','partial') and scoped(request.user,m.Purchase,action='receive').filter(pk=obj.pk).exists()
        context['receipt_lines']=obj.lines.select_related('material').all()
    if isinstance(obj,m.Handoff):
        context['handoff_payload']=obj.payload
        context['can_accept']=obj.status=='sent' and can(request.user,'handoffs','accept',obj.target_organization_id)
    if isinstance(obj,m.Design):
        context['can_revision']=scoped(request.user,m.Design,action='edit').filter(pk=obj.pk).exists()
        context['design_versions']=scoped(request.user,m.Design).filter(family_id=obj.family_id).order_by('-revision')[:20]
    if isinstance(obj,m.ChangeRequest):
        context['change_lines']=obj.lines.select_related('order_line').all()
        form=forms.modelform_factory(m.ChangeLine,fields=['operation','order_line','name','room','specification','quantity','unit','unit_price','unit_cost'])()
        form.fields['order_line'].queryset=obj.order.lines.all()
        if not can(request.user,'finance','view',obj.organization_id):form.fields.pop('unit_cost')
        style_fields(form)
        context['change_form']=form
        context['can_change_lines']=obj.status=='pending' and scoped(request.user,m.ChangeRequest,action='edit').filter(pk=obj.pk).exists()
    if obj.module in evidence.PARENTS:
        context['attachments']=m.Attachment.objects.filter(organization=obj.organization,object_type=obj.module,object_id=obj.pk).select_related('owner')
        context['can_attach']=scoped(request.user,type(obj),action='edit').filter(pk=obj.pk).exists()
    context['today_iso']=str(timezone.localdate())
    return context


@login_required
@require_POST
def upload(request,key,pk):
    obj=record_for(request,module_for(key),pk)
    try:
        if 'file' not in request.FILES:raise ValidationError('请选择文件。')
        evidence.upload_attachment(request.user,obj,request.FILES['file'],request.POST.get('kind','drawing'))
        messages.success(request,'附件已私有保存，下载将实时校验业务权限。')
    except BUSINESS_ERRORS as exc:messages.error(request,error_message(exc))
    return redirect('detail',key=key,pk=pk)


@login_required
def download(request,pk):
    attachment=evidence.authorized_attachment(request.user,pk)
    try:response=FileResponse(attachment.file.open('rb'),as_attachment=True,filename=attachment.original_name)
    except FileNotFoundError:raise Http404('附件文件不存在，请联系管理员恢复备份。')
    response['X-Content-Type-Options']='nosniff'
    response['Cache-Control']='private, no-store'
    return response


@login_required
@require_POST
def feature_action(request,key,pk,operation):
    obj=record_for(request,module_for(key),pk)
    version=request.POST.get('version')
    try:
        if operation=='revision' and isinstance(obj,m.Design):
            revised=daily.new_design_revision(request.user,obj,version)
            return redirect('detail',key='designs',pk=revised.pk)
        elif operation=='handoff' and isinstance(obj,m.Order):
            target=get_object_or_404(m.Organization,pk=request.POST.get('target'),kind='factory')
            result=collaboration.send_handoff(request.user,obj,target,version,request.POST.get('note',''))
            return redirect('detail',key='handoffs',pk=result.pk)
        elif operation in ('accept','reject') and isinstance(obj,m.Handoff):
            result=collaboration.accept_handoff(request.user,obj,version,reject=operation=='reject',note=request.POST.get('note',''))
            if operation=='accept':return redirect('detail',key='orders',pk=result.pk)
        elif operation in ('reserve','unreserve') and isinstance(obj,m.Production):
            materials.reserve_production(request.user,obj,version,release=operation=='unreserve')
        elif operation=='plan' and isinstance(obj,m.Order):
            try:first_due=date.fromisoformat(request.POST.get('first_due',''))
            except ValueError:raise ValidationError('请填写有效到期日。')
            receivables.create_plan(request.user,obj,version,first_due,[request.POST.get('p1','30'),request.POST.get('p2','40'),request.POST.get('p3','30')])
        elif operation=='receive' and isinstance(obj,m.Purchase):
            quantities={name[4:]:value for name,value in request.POST.items() if name.startswith('qty_') and value.strip() and value!='0'}
            materials.receive_partial(request.user,obj,quantities,request.POST.get('reference',''),version)
        elif operation=='change-line' and isinstance(obj,m.ChangeRequest):
            Form=forms.modelform_factory(m.ChangeLine,fields=['operation','order_line','name','room','specification','quantity','unit','unit_price','unit_cost'])
            form=Form(request.POST)
            form.fields['order_line'].queryset=obj.order.lines.all()
            if not can(request.user,'finance','view',obj.organization_id):form.fields.pop('unit_cost')
            if not form.is_valid():raise ValidationError([str(e) for errors in form.errors.values() for e in errors])
            pricing.add_change_line(request.user,obj,form.cleaned_data,int(version))
        else:raise Http404
        messages.success(request,'操作已完成，相关业务记录已同步。')
    except (*BUSINESS_ERRORS,ValueError,TypeError) as exc:
        messages.error(request,error_message(exc) if isinstance(exc,BUSINESS_ERRORS) else '提交参数不完整，请刷新后重试。')
    return redirect('detail',key=key,pk=pk)


@login_required
def print_record(request,key,pk):
    if key not in ('quotes','orders','deliveries'):raise Http404
    obj=record_for(request,module_for(key),pk)
    document=obj.order if isinstance(obj,m.Delivery) else obj
    return render(request,'erp/print.html',{'module':module_for(key),'obj':obj,'document':document,
                                           'lines':document.lines.all(),'is_quote':isinstance(document,m.Quote),
                                           'is_delivery':key=='deliveries'})


@login_required
def import_csv(request,key):
    if key not in daily.IMPORT_COLUMNS:raise Http404
    module=module_for(key)
    if not can(request.user,key,'create',active_org(request)):raise PermissionDenied
    if request.GET.get('template')=='1':
        result=HttpResponse(content_type='text/csv; charset=utf-8')
        result['Content-Disposition']=f'attachment; filename="{key}-template.csv"'
        result.write('\ufeff'); csv.writer(result).writerow(daily.IMPORT_COLUMNS[key]); return result
    class ImportForm(forms.Form):
        organization=forms.ModelChoiceField(label='导入组织',queryset=accessible_organizations(request.user,key,'create'))
        file=forms.FileField(label='UTF-8 CSV 文件（最大1MB / 1000行）')
    form=ImportForm(request.POST or None,request.FILES or None);style_fields(form)
    if request.method=='POST' and form.is_valid():
        try:
            count=daily.import_records(request.user,key,form.cleaned_data['organization'],form.cleaned_data['file'])
            messages.success(request,f'已导入{count}条记录。')
            return redirect('list',key=key)
        except BUSINESS_ERRORS as exc:form.add_error(None,error_message(exc))
    return render(request,'erp/import.html',{'form':form,'module':module})
