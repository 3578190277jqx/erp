"""Dedicated screens/actions for commerce and collaboration beyond generic CRUD."""
import csv
import io
from datetime import date
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.http import FileResponse,HttpResponse,Http404,JsonResponse
from django.shortcuts import render,redirect,get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST,require_GET
from erp import models as m
from erp.permissions import scoped,can,accessible_organizations
from erp.forms import style_fields,ChangeLineForm
from erp.services import evidence,collaboration,materials,receivables,pricing,daily
from .modules import record_for,module_for,active_org,error_message,BUSINESS_ERRORS


def feature_context(request,obj):
    context={}
    if isinstance(obj,(m.Quote,m.Order)):
        if isinstance(obj,m.Quote):
            amounts=pricing.quote_amounts(obj)
        else:
            order_lines=list(obj.lines.all())
            net=sum((line.line_total for line in order_lines),Decimal(0))
            cost=sum((pricing.rounded(line.billable_quantity*line.unit_cost) for line in order_lines),Decimal(0))
            amounts={'net':net,'tax':pricing.rounded(net*obj.tax_rate),'cost':cost,'margin':net-cost}
            context['snapshot_mismatch']=obj.total!=net+amounts['tax']
            context['order_lines']=order_lines
        context['amounts']=amounts
        context['show_costs']=can(request.user,'finance','view',obj.organization_id)
    if isinstance(obj,m.Order):
        context['pricing_unknown']=obj.kind=='sale' and not obj.discount_confirmed
        context['plans']=receivables.plan_rows(obj)
        context['can_plan']=obj.kind=='sale' and obj.status=='confirmed' and not context['plans'] and (
            can(request.user,'finance','create',obj.organization_id) or can(request.user,'changes','approve',obj.organization_id))
        context['handoff']=m.Handoff.objects.filter(order=obj).first()
        context['can_handoff']=obj.kind=='sale' and obj.status=='confirmed' and scoped(request.user,m.Order,action='edit').filter(pk=obj.pk).exists() and (not context['handoff'] or context['handoff'].status in ('rejected','withdrawn'))
        context['factories']=collaboration.allowed_factories(request.user,obj.organization)
        context['can_withdraw_handoff']=bool(context['handoff'] and context['handoff'].status=='sent' and collaboration.can_withdraw_handoff(request.user,context['handoff']))
    if isinstance(obj,m.Production):
        context['can_reserve']=obj.status=='planned' and scoped(request.user,m.Production,action='reserve').filter(pk=obj.pk).exists()
        context['reservations']=obj.reservations.select_related('stock__material').all()
    if isinstance(obj,m.Purchase):
        context['can_receive']=obj.status in ('ordered','partial') and scoped(request.user,m.Purchase,action='receive').filter(pk=obj.pk).exists()
        context['receipt_lines']=obj.lines.select_related('material').all()
    if isinstance(obj,m.Handoff):
        context['handoff_payload']=obj.payload
        context['route_inactive']=not m.CollaborationRoute.objects.filter(source_id=obj.organization_id,target_id=obj.target_organization_id,enabled=True).exists()
    if isinstance(obj,m.Design):
        context['can_revision']=scoped(request.user,m.Design,action='edit').filter(pk=obj.pk).exists()
        context['design_versions']=scoped(request.user,m.Design).filter(family_id=obj.family_id).order_by('-revision')[:20]
    if isinstance(obj,m.ChangeRequest):
        change_lines=list(obj.lines.select_related('order_line').all())
        for line in change_lines:
            line.display_net=line.net_amount
            if line.display_net is None and obj.status=='pending':line.display_net=pricing.line_net(line,pricing.change_discount(obj))
        context['change_lines']=change_lines
        if obj.status=='pending':
            source=list(obj.order.lines.all())
            source_total=sum((line.line_total for line in source),Decimal(0))
            source_total+=pricing.rounded(source_total*obj.order.tax_rate)
            summary={'old_total':obj.order.total,'new_total':obj.proposed_total,'delta':obj.proposed_total-obj.order.total,
                     'source_count':len(source),'operation_count':len(change_lines),'restore':obj.restore_product_snapshot,
                     'snapshot_total':source_total,'mismatch':bool(source and source_total!=obj.order.total),
                     'reprices':bool(change_lines or obj.proposed_discount is not None)}
            summary['proposed_count']=None
            try:summary['proposed_count']=len(pricing.proposed_lines(obj))
            except ValidationError as exc:summary['error']='；'.join(exc.messages)
            delta=summary['delta']
            summary['delta_display']=f'+¥{delta:,.2f}' if delta>0 else f'−¥{-delta:,.2f}' if delta<0 else '¥0.00'
            summary['version_changed']=obj.order_version!=obj.order.version
            context['change_review']=summary
            copy=(f"原合同金额 ¥{obj.order.total:,.2f} → 拟合同金额 ¥{obj.proposed_total:,.2f}。"
                  f"原合同产品 {len(source)} 条，本次操作 {len(change_lines)} 条，拟产品 {summary['proposed_count'] if summary['proposed_count'] is not None else '待核对'} 条。")
            if summary['reprices']:copy+='本次将按全部拟产品重算合同金额。'
            if summary['mismatch']:copy+='产品快照合计与合同金额不一致，请核对本次改价影响。'
            if summary['restore']:copy+='完整性需人工核对：请确认已补录全部原产品和本次增减项。'
            context['change_approval_copy']=copy

        if obj.status=='pending' and obj.revise_payment_plan and obj.proposed_total!=obj.order.total:
            rows=receivables.plan_rows(obj.order)
            try:
                values=receivables.revised_amounts(obj.proposed_total,rows)
                context['plan_revision_preview']=[dict(row,new_amount=value,new_balance=value-row['paid']) for row,value in zip(rows,values)]
            except ValidationError as exc:context['plan_revision_error']='；'.join(exc.messages)

        form=ChangeLineForm(request.user,obj)
        fields=[name for name in pricing.SNAPSHOT_FIELDS if name in form.fields]
        context['change_source_lines']={str(line.pk):{name:str(getattr(line,name)) for name in fields} for line in obj.order.lines.all()}
        context['change_form']=form
        context['can_change_lines']=obj.status=='pending' and scoped(request.user,m.ChangeRequest,action='edit').filter(pk=obj.pk).exists()
    if obj.module in evidence.PARENTS:
        context['attachments'],rights,context['evidence_stale']=evidence.attachment_context(request.user,obj)
        for item in context['attachments']:
            item.can_delete=evidence.can_delete_evidence(request.user,obj,item,rights)
            item.can_stop_sharing=evidence.can_stop_sharing(request.user,obj,item,rights)
            item.stop_share_label='停止向工厂共享' if rights.archived_read else '撤回我的文件'
        context['can_attach']=rights.write
        context['shared_evidence']=isinstance(obj,m.Handoff)
    context['today_iso']=str(timezone.localdate())
    return context


@login_required
@require_POST
def upload(request,key,pk):
    obj=record_for(request,module_for(key),pk)
    try:
        if 'file' not in request.FILES:raise ValidationError('请选择文件。')
        evidence.upload_attachment(request.user,obj,request.FILES['file'],request.POST.get('kind','drawing'),share_confirmed=request.POST.get('share_confirmed')=='on',
            parent_version=request.POST.get('parent_version'),target_id=request.POST.get('target_id'))
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
def delete_attachment(request,pk):
    item=evidence.authorized_attachment(request.user,pk)
    try:
        evidence.delete_attachment(request.user,item,request.POST.get('version'))
        messages.success(request,'附件已删除，保留审计记录。')
    except BUSINESS_ERRORS as exc:messages.error(request,error_message(exc))
    return redirect('detail',key=item.object_type,pk=item.object_id)


@login_required
@require_POST
def stop_sharing(request,pk):
    item=evidence.authorized_attachment(request.user,pk)
    try:
        evidence.stop_sharing_attachment(request.user,item,request.POST.get('version'),request.POST.get('parent_version'),request.POST.get('note',''))
        messages.success(request,'已停止共享，文件保留归档；已下载的副本无法从对方设备收回。')
    except BUSINESS_ERRORS as exc:messages.error(request,error_message(exc))
    return redirect('detail',key=item.object_type,pk=item.object_id)


@login_required
@require_GET
def payment_plan_choices(request):
    try:pk=int(request.GET.get('order',''))
    except (ValueError,TypeError):raise Http404
    order=record_for(request,module_for('orders'),pk)
    if not can(request.user,'finance','create',order.organization_id):raise PermissionDenied
    rows=[{'id':row['plan'].pk,'label':f'{row["plan"].sequence} · {row["plan"].name} · 未收 {row["balance"]:.2f}'} for row in receivables.plan_rows(order)]
    response=JsonResponse({'plans':rows});response['Cache-Control']='private, no-store';return response


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
            target=get_object_or_404(collaboration.allowed_factories(request.user,obj.organization),pk=request.POST.get('target'))
            result=collaboration.send_handoff(request.user,obj,target,version,request.POST.get('note',''),request.POST.get('handoff_version'))
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
        elif operation=='remove-change-line' and isinstance(obj,m.ChangeRequest):
            pricing.remove_change_line(request.user,obj,request.POST.get('line_id'),version)
        elif operation=='change-line' and isinstance(obj,m.ChangeRequest):
            form=ChangeLineForm(request.user,obj,request.POST)
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
