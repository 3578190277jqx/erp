from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from . import models as m
from .permissions import accessible_organizations, scoped, member_orgs, role_allows


def style_fields(form):
    for name, field in form.fields.items():
        if isinstance(field.widget, forms.HiddenInput):
            continue
        field.widget.attrs['class'] = 'form-check-input' if isinstance(field.widget,forms.CheckboxInput) else ('form-select' if isinstance(field.widget, forms.Select) else 'form-control')
        if isinstance(field, forms.DateField):
            field.widget = forms.DateInput(format='%Y-%m-%d', attrs={'type': 'date', 'class': 'form-control'})
        if isinstance(field.widget, forms.Textarea):
            field.widget.attrs['rows'] = 3
        if field.required:
            field.widget.attrs['aria-required'] = 'true'


def selected_org(user, module, data, instance, active_org):
    if instance and instance.pk:
        return instance.organization
    allowed = accessible_organizations(user, module, 'create')
    selected = data.get('organization') if data else active_org
    try:
        return allowed.filter(pk=int(selected)).first() if selected else allowed.first()
    except (ValueError, TypeError):
        return None


def responsible_users(user, module, organization):
    User = get_user_model()
    if user.is_superuser:
        return User.objects.filter(is_active=True)
    if not organization:
        return User.objects.none()
    own_only = not any(member.scope != 'own' and organization.pk in member_orgs(member)
                       and role_allows(member.role, module, 'edit') for member in user.memberships.all())
    if own_only:
        return User.objects.filter(pk=user.pk)
    ids = {user.pk}
    for member in m.Membership.objects.select_related('user'):
        if organization.pk in member_orgs(member) and role_allows(member.role, module, 'view'):
            ids.add(member.user_id)
    return User.objects.filter(pk__in=ids, is_active=True)


def document_form(module, user, data=None, instance=None, active_org=None):
    fields = list(module.fields) + ['owner']
    result_entry = isinstance(instance, m.Measurement) and instance.status == 'in_progress'
    if result_entry:
        fields = ['area', 'note']
    if not instance or not instance.pk:
        fields.insert(0, 'organization')
    Form = forms.modelform_factory(module.model, fields=fields)
    form = Form(data=data, instance=instance)
    if isinstance(form.instance,m.Design):
        form.fields['revision'].disabled=True
    if isinstance(form.instance,m.Purchase):
        form.fields['supplier'].required=False
    form.fields['version'] = forms.IntegerField(widget=forms.HiddenInput(), initial=instance.version if instance and instance.pk else 1)
    form.fields['version'].required = bool(instance and instance.pk)
    org = selected_org(user, module.key if module.key != 'warehouses' else 'inventory', data, instance, active_org)
    if 'organization' in form.fields:
        form.fields['organization'].queryset = accessible_organizations(user, module.model.module, 'create')
        form.fields['organization'].initial = org.pk if org else None
        form.fields['organization'].widget.attrs['data-org-select'] = 'true'
    form.instance.organization = org
    if 'owner' in form.fields:
        form.fields['owner'].queryset = responsible_users(user, module.model.module, org)
        form.fields['owner'].initial = user.pk
        form.fields['owner'].label_from_instance = lambda person: f'{person.first_name or person.username} · {person.username}'
    for name, field in form.fields.items():
        if isinstance(field, forms.ModelChoiceField) and name not in ('organization', 'owner'):
            field.queryset = scoped(user, field.queryset.model, org.pk if org else -1)
            if field.queryset.model==m.Customer:field.queryset=field.queryset.filter(is_internal=False)
    if isinstance(form.instance, m.Order) and form.instance.quote_id:
        form.fields['total'].disabled = True
        form.fields['customer'].disabled = True
    if isinstance(form.instance, m.ChangeRequest):
        form.instance.order_version = 1
        if data and data.get('order'):
            order = form.fields['order'].queryset.filter(pk=data.get('order')).first()
            if order:
                form.instance.order_version = order.version
    style_fields(form)
    return form


def line_form(user, document, data=None):
    if isinstance(document, m.Quote):
        model, fields = m.QuoteLine, ['name','room','specification','category','pricing_mode','width_mm','height_mm',
                                    'quantity','unit','unit_price','unit_cost','discountable']
    elif isinstance(document, m.Breakdown):
        model, fields = m.BOMLine, ['order_line','material', 'quantity', 'waste']
    else:
        model, fields = m.PurchaseLine, ['material', 'quantity', 'unit_price']
    Form = forms.modelform_factory(model, fields=fields)
    form = Form(data=data)
    if isinstance(document,m.Quote):
        form.fields['quantity'].help_text='按尺寸计价填件数；展开面积填实测㎡；数量和套餐填套数。'
        form.fields['width_mm'].help_text='投影面积填宽度；延米填长度，单位为mm。'
        form.fields['unit'].help_text='投影 / 展开面积填㎡，延米填m，费用项可填项。'
    if 'material' in form.fields:
        form.fields['material'].queryset = scoped(user, m.Material, document.organization_id)
    if 'unit_cost' in form.fields:
        from .permissions import can
        if not can(user,'finance','view',document.organization_id):form.fields.pop('unit_cost')
    if 'order_line' in form.fields:
        form.fields['order_line'].queryset=document.order.lines.all()
    form.fields['version'] = forms.IntegerField(widget=forms.HiddenInput(), initial=document.version)
    style_fields(form)
    return form


class StockAdjustmentForm(forms.Form):
    warehouse = forms.ModelChoiceField(label='仓库', queryset=m.Warehouse.objects.none())
    material = forms.ModelChoiceField(label='物料', queryset=m.Material.objects.none())
    quantity = forms.DecimalField(label='调整数量（负数为出库）', max_digits=12, decimal_places=3)
    reason = forms.CharField(label='调整原因', max_length=500, widget=forms.Textarea)

    def __init__(self, user, org=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['warehouse'].queryset = scoped(user, m.Warehouse, org, action='adjust')
        self.fields['material'].queryset = scoped(user, m.Material, org)
        style_fields(self)


class PaymentForm(forms.Form):
    direction = forms.ChoiceField(label='收支方向', choices=[('in', '收款'), ('out', '付款')])
    order = forms.ModelChoiceField(label='应收订单（收款时选择）', queryset=m.Order.objects.none(), required=False)
    purchase = forms.ModelChoiceField(label='应付采购（付款时选择）', queryset=m.Purchase.objects.none(), required=False)
    plan = forms.ModelChoiceField(label='指定收款期次（不选则按顺序分摊）',queryset=m.PaymentPlan.objects.none(),required=False)
    amount = forms.DecimalField(label='本次金额', max_digits=14, decimal_places=2, min_value=0.01)
    reference = forms.CharField(label='唯一凭证号', max_length=80)
    paid_date = forms.DateField(label='收付日期')
    method = forms.CharField(label='收付方式', max_length=40, initial='银行转账')
    note = forms.CharField(label='备注', required=False, widget=forms.Textarea)

    def __init__(self, user, org=None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = accessible_organizations(user, 'finance', 'create')
        self.fields['order'].queryset = scoped(user, m.Order, org).filter(organization__in=allowed).exclude(status__in=['draft', 'cancelled'])
        self.fields['purchase'].queryset = scoped(user, m.Purchase, org).filter(organization__in=allowed, status__in=['ordered','partial', 'received'])
        data=kwargs.get('data') or {}
        selected=data.get('order') or self.initial.get('order')
        try:selected=int(selected)
        except (TypeError,ValueError):selected=None
        self.fields['plan'].queryset=scoped(user,m.PaymentPlan,org).filter(order__in=self.fields['order'].queryset,order_id=selected).select_related('order')
        self.fields['plan'].label_from_instance=lambda plan:f'{plan.order.code} · {plan.sequence} {plan.name} · 应收{plan.amount:.2f}'
        from django.urls import reverse
        self.fields['order'].widget.attrs['data-plan-url']=reverse('payment_plan_choices')
        style_fields(self)

    def clean(self):
        data = super().clean()
        incoming = data.get('direction') == 'in'
        if (incoming and (not data.get('order') or data.get('purchase'))) or (not incoming and (not data.get('purchase') or data.get('order'))):
            raise ValidationError('收款只选择订单；付款只选择采购，不能同时选择。')
        if data.get('plan') and (not incoming or data['plan'].order_id!=data['order'].pk):
            raise ValidationError('指定期次必须属于当前应收订单。')
        return data


class ChangeLineForm(forms.ModelForm):
    class Meta:
        model=m.ChangeLine
        fields=['operation','order_line','name','room','specification','pricing_mode','category','width_mm','height_mm',
                'quantity','unit','unit_price','unit_cost','discountable']

    def __init__(self,user,change,data=None,**kwargs):
        from .permissions import can
        from .services.pricing import SNAPSHOT_FIELDS
        operation=data.get('operation') if data else kwargs.get('initial',{}).get('operation','add')
        if data is not None and operation=='update':
            data=data.copy()
            try:target=change.order.lines.filter(pk=int(data.get('order_line',''))).first()
            except (TypeError,ValueError):target=None
            if target:
                for name in SNAPSHOT_FIELDS:
                    if name not in data:data[name]=getattr(target,name)
        super().__init__(data=data,**kwargs)
        self.fields['order_line'].queryset=change.order.lines.all()
        self.fields['discountable']=forms.TypedChoiceField(label='参与合同折扣',choices=[('True','是'),('False','否')],
                                  coerce=lambda value:value=='True',initial='True')
        if not can(user,'finance','view',change.organization_id):self.fields.pop('unit_cost')
        if operation=='remove':
            for name in SNAPSHOT_FIELDS:self.fields.pop(name,None)
        else:
            self.fields['name'].required=True
            self.fields['quantity'].label='数量 / 件数（展开面积填㎡）'
        style_fields(self)
