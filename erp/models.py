import uuid
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models


def money_field(label, default=0):
    return models.DecimalField(label, max_digits=14, decimal_places=2, default=default,
                               validators=[MinValueValidator(Decimal('0'))])


def qty_field(label, default=1):
    return models.DecimalField(label, max_digits=12, decimal_places=3, default=default,
                               validators=[MinValueValidator(Decimal('0.001'))])


class Organization(models.Model):
    KINDS = [('company', '公司'), ('store', '门店'), ('factory', '工厂'), ('warehouse', '仓储组织')]
    name = models.CharField('组织名称', max_length=100)
    kind = models.CharField('组织类型', max_length=20, choices=KINDS, default='store')
    parent = models.ForeignKey('self', verbose_name='上级组织', null=True, blank=True,
                               on_delete=models.PROTECT, related_name='children')

    class Meta:
        verbose_name = '组织'
        verbose_name_plural = '组织'

    def __str__(self):
        return self.name

    def clean(self):
        seen = {self.pk} if self.pk else set()
        current = self.parent
        while current:
            if current.pk in seen:
                raise ValidationError({'parent': '组织层级不能形成循环。'})
            seen.add(current.pk)
            current = current.parent


ROLES = [
    ('boss', '老板'), ('manager', '店长'), ('sales', '导购'), ('designer', '设计师'),
    ('surveyor', '量尺员'), ('engineer', '拆单员'), ('planner', '生产计划'),
    ('buyer', '采购'), ('warehouse', '仓管'), ('logistics', '物流'),
    ('installer', '安装师傅'), ('service', '售后'), ('finance', '财务'), ('dealer', '经销商'),
]


class Membership(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='memberships', verbose_name='用户')
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, verbose_name='组织')
    role = models.CharField('角色', max_length=20, choices=ROLES)
    scope = models.CharField('数据范围', max_length=10, default='own',
                             choices=[('own', '本人负责'), ('org', '本组织'), ('tree', '本组织及下级')])

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'organization', 'role'], name='unique_membership')]
        verbose_name = '组织成员'
        verbose_name_plural = '组织成员'

    def __str__(self):
        return f'{self.user} · {self.organization} · {self.get_role_display()}'


class Record(models.Model):
    code = models.CharField('单据编号', max_length=32, unique=True, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT, verbose_name='所属组织')
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name='负责人')
    version = models.PositiveIntegerField('版本', default=1, editable=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    note = models.TextField('备注 / 处理结论', blank=True, max_length=5000)

    class Meta:
        abstract = True
        ordering = ['-created_at']

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = f'{self.__class__.__name__[:3].upper()}-{uuid.uuid4().hex[:10].upper()}'
        super().save(*args, **kwargs)

    def __str__(self):
        title = getattr(self, 'name', None) or getattr(self, 'title', None)
        return f'{self.code} · {title}' if title else self.code

    def clean(self):
        super().clean()
        if not self.organization_id:
            return
        errors = {}
        for field in self._meta.fields:
            if not isinstance(field, models.ForeignKey) or field.name in ('organization', 'owner'):
                continue
            if getattr(self, field.attname, None):
                obj = getattr(self, field.name)
                if hasattr(obj, 'organization_id') and obj.organization_id != self.organization_id:
                    errors[field.name] = '关联单据必须属于同一组织业务域。'
        if errors:
            raise ValidationError(errors)


class Customer(Record):
    module = 'customers'
    name = models.CharField('客户名称', max_length=100)
    phone = models.CharField('联系电话', max_length=30)
    address = models.CharField('项目地址', max_length=250, blank=True)
    source = models.CharField('线索来源', max_length=40, default='门店到访')
    status = models.CharField('跟进阶段', max_length=20, default='new', choices=[
        ('new', '新线索'), ('contacted', '跟进中'), ('measuring', '量尺中'),
        ('designing', '设计中'), ('quoted', '已报价'), ('won', '已成交'), ('lost', '已关闭')])
    due_date = models.DateField('下次跟进', null=True, blank=True)


class Measurement(Record):
    module = 'measurements'
    customer = models.ForeignKey(Customer, verbose_name='客户', on_delete=models.PROTECT)
    due_date = models.DateField('预约日期')
    area = models.DecimalField('实测面积（㎡）', max_digits=10, decimal_places=2, null=True, blank=True,
                               validators=[MinValueValidator(Decimal('0.01'))])
    status = models.CharField('状态', max_length=20, default='pending', choices=[
        ('pending', '待分配'), ('assigned', '已分配'), ('in_progress', '进行中'),
        ('completed', '已完成'), ('failed', '量尺失败')])


class Design(Record):
    module = 'designs'
    customer = models.ForeignKey(Customer, verbose_name='客户', on_delete=models.PROTECT)
    title = models.CharField('方案名称', max_length=150)
    revision = models.PositiveIntegerField('方案版本', default=1)
    status = models.CharField('状态', max_length=20, default='draft', choices=[
        ('draft', '草稿'), ('review', '待审核'), ('approved', '已批准'), ('rejected', '已驳回')])


class Quote(Record):
    module = 'quotes'
    customer = models.ForeignKey(Customer, verbose_name='客户', on_delete=models.PROTECT)
    design = models.ForeignKey(Design, verbose_name='关联设计', on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField('报价名称', max_length=150)
    discount = models.DecimalField('折扣系数（0—1）', max_digits=5, decimal_places=4, default=1,
                                   validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('1'))])
    total = money_field('含折扣总额')
    status = models.CharField('状态', max_length=20, default='draft', choices=[
        ('draft', '草稿'), ('review', '待审核'), ('approved', '已批准'),
        ('rejected', '已驳回'), ('accepted', '已接受')])


class QuoteLine(models.Model):
    quote = models.ForeignKey(Quote, on_delete=models.CASCADE, related_name='lines')
    name = models.CharField('产品名称', max_length=120)
    room = models.CharField('空间', max_length=60, blank=True)
    specification = models.CharField('规格 / 材质', max_length=200, blank=True)
    quantity = qty_field('数量')
    unit = models.CharField('单位', max_length=10, default='套')
    unit_price = money_field('单价')

    @property
    def subtotal(self):
        return (self.quantity * self.unit_price).quantize(Decimal('0.01'))

    class Meta:
        ordering = ['id']
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0, unit_price__gte=0), name='valid_quote_line')]


class Order(Record):
    module = 'orders'
    customer = models.ForeignKey(Customer, verbose_name='客户', on_delete=models.PROTECT)
    quote = models.OneToOneField(Quote, verbose_name='来源报价', on_delete=models.PROTECT, null=True, blank=True)
    title = models.CharField('项目名称', max_length=150)
    contract_number = models.CharField('合同编号', max_length=80, blank=True)
    due_date = models.DateField('承诺交期', null=True, blank=True)
    total = money_field('合同金额')
    status = models.CharField('履约状态', max_length=20, default='draft', choices=[
        ('draft', '合同待确认'), ('confirmed', '已确认'), ('producing', '生产中'),
        ('ready', '待配送'), ('shipping', '配送中'), ('installing', '待安装'),
        ('completed', '已完成'), ('cancelled', '已取消')])


class Material(Record):
    module = 'materials'
    name = models.CharField('物料名称', max_length=100)
    specification = models.CharField('规格', max_length=150, blank=True)
    unit = models.CharField('单位', max_length=10, default='张')
    unit_cost = money_field('参考成本')
    safety_stock = models.DecimalField('安全库存', max_digits=12, decimal_places=3, default=10,
                                       validators=[MinValueValidator(Decimal('0'))])


class Warehouse(Record):
    module = 'inventory'
    name = models.CharField('仓库名称', max_length=100)
    address = models.CharField('仓库地址', max_length=250, blank=True)


class Breakdown(Record):
    module = 'breakdowns'
    order = models.OneToOneField(Order, verbose_name='合同订单', on_delete=models.PROTECT, related_name='breakdown')
    quantity = qty_field('生产套数')
    status = models.CharField('状态', max_length=20, default='draft', choices=[
        ('draft', '编制中'), ('review', '待审核'), ('released', '已释放')])


class BOMLine(models.Model):
    breakdown = models.ForeignKey(Breakdown, on_delete=models.CASCADE, related_name='lines')
    material = models.ForeignKey(Material, verbose_name='物料', on_delete=models.PROTECT)
    quantity = qty_field('单位用量')
    waste = models.DecimalField('损耗率（0—1）', max_digits=5, decimal_places=4, default=0,
                                validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('1'))])

    @property
    def required(self):
        return (self.quantity * self.breakdown.quantity * (1 + self.waste)).quantize(Decimal('.001'))

    class Meta:
        ordering = ['id']
        constraints = [models.UniqueConstraint(fields=['breakdown', 'material'], name='unique_bom_material'),
                       models.CheckConstraint(condition=models.Q(quantity__gt=0, waste__gte=0, waste__lte=1), name='valid_bom_line')]


class Production(Record):
    module = 'production'
    breakdown = models.OneToOneField(Breakdown, verbose_name='拆单 / BOM', on_delete=models.PROTECT, related_name='production')
    warehouse = models.ForeignKey(Warehouse, verbose_name='领料仓库', on_delete=models.PROTECT, null=True, blank=True)
    work_center = models.CharField('工作中心', max_length=80, default='定制生产一线')
    start_date = models.DateField('计划开工', null=True, blank=True)
    due_date = models.DateField('计划完工', null=True, blank=True)
    status = models.CharField('状态', max_length=20, default='pending', choices=[
        ('pending', '待排程'), ('planned', '已排程'), ('issued', '已领料'),
        ('in_progress', '生产中'), ('completed', '已完工')])

    def clean(self):
        super().clean()
        if self.start_date and self.due_date and self.start_date > self.due_date:
            raise ValidationError({'due_date': '完工日期不能早于开工日期。'})


class Purchase(Record):
    module = 'purchases'
    supplier = models.CharField('供应商', max_length=150)
    warehouse = models.ForeignKey(Warehouse, verbose_name='收货仓库', on_delete=models.PROTECT)
    due_date = models.DateField('预计到货', null=True, blank=True)
    total = money_field('采购总额')
    status = models.CharField('状态', max_length=20, default='draft', choices=[
        ('draft', '草稿'), ('review', '待审核'), ('approved', '已批准'),
        ('ordered', '已下单'), ('received', '已收货')])


class PurchaseLine(models.Model):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name='lines')
    material = models.ForeignKey(Material, verbose_name='采购物料', on_delete=models.PROTECT)
    quantity = qty_field('采购数量')
    unit_price = money_field('采购单价')

    @property
    def subtotal(self):
        return (self.quantity * self.unit_price).quantize(Decimal('.01'))

    class Meta:
        ordering = ['id']
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0, unit_price__gte=0), name='valid_purchase_line')]


class Stock(Record):
    module = 'inventory'
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, verbose_name='仓库')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, verbose_name='物料')
    quantity = models.DecimalField('现存量', max_digits=14, decimal_places=3, default=0,
                                   validators=[MinValueValidator(Decimal('0'))])

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['warehouse', 'material'], name='unique_stock_location'),
                       models.CheckConstraint(condition=models.Q(quantity__gte=0), name='nonnegative_stock')]


class StockMovement(Record):
    module = 'movements'
    warehouse = models.ForeignKey(Warehouse, on_delete=models.PROTECT, verbose_name='仓库')
    material = models.ForeignKey(Material, on_delete=models.PROTECT, verbose_name='物料')
    delta = models.DecimalField('变动量', max_digits=14, decimal_places=3)
    balance = models.DecimalField('结存', max_digits=14, decimal_places=3)
    reference = models.CharField('业务凭证', max_length=100)

    class Meta(Record.Meta):
        constraints = [models.UniqueConstraint(fields=['reference', 'material', 'warehouse'], name='unique_movement_reference')]


class Delivery(Record):
    module = 'deliveries'
    order = models.OneToOneField(Order, verbose_name='订单', on_delete=models.PROTECT, related_name='delivery')
    due_date = models.DateField('配送日期')
    vehicle = models.CharField('车牌 / 物流单号', max_length=80, blank=True)
    status = models.CharField('状态', max_length=20, default='pending', choices=[
        ('pending', '待派车'), ('assigned', '已派车'), ('shipped', '运输中'), ('delivered', '已签收')])


class Installation(Record):
    module = 'installations'
    order = models.OneToOneField(Order, verbose_name='订单', on_delete=models.PROTECT, related_name='installation')
    due_date = models.DateField('安装日期')
    status = models.CharField('状态', max_length=20, default='pending', choices=[
        ('pending', '待分配'), ('assigned', '已分配'), ('in_progress', '安装中'),
        ('completed', '已验收'), ('failed', '安装失败')])


class AfterSales(Record):
    module = 'aftersales'
    order = models.ForeignKey(Order, verbose_name='订单', on_delete=models.PROTECT)
    title = models.CharField('问题描述', max_length=180)
    priority = models.CharField('优先级', max_length=15, default='normal', choices=[('normal', '普通'), ('high', '紧急')])
    due_date = models.DateField('处理期限')
    status = models.CharField('状态', max_length=20, default='new', choices=[
        ('new', '待处理'), ('in_progress', '处理中'), ('resolved', '已解决'), ('closed', '已关闭')])


class Payment(Record):
    module = 'finance'
    direction = models.CharField('收支方向', max_length=10, choices=[('in', '收款'), ('out', '付款')])
    order = models.ForeignKey(Order, verbose_name='应收订单', on_delete=models.PROTECT, null=True, blank=True)
    purchase = models.ForeignKey(Purchase, verbose_name='应付采购', on_delete=models.PROTECT, null=True, blank=True)
    amount = money_field('金额')
    paid_date = models.DateField('收付日期')
    method = models.CharField('收付方式', max_length=40, default='银行转账')
    reference = models.CharField('唯一凭证号', max_length=80, unique=True)

    class Meta(Record.Meta):
        constraints = [models.CheckConstraint(condition=(models.Q(direction='in', order__isnull=False, purchase__isnull=True)
                       | models.Q(direction='out', purchase__isnull=False, order__isnull=True)), name='payment_single_document'),
                       models.CheckConstraint(condition=models.Q(amount__gt=0), name='positive_payment')]

    def clean(self):
        super().clean()
        if self.direction == 'in' and (not self.order_id or self.purchase_id):
            raise ValidationError('收款必须且只能关联应收订单。')
        if self.direction == 'out' and (not self.purchase_id or self.order_id):
            raise ValidationError('付款必须且只能关联应付采购。')


class ChangeRequest(Record):
    module = 'changes'
    order = models.ForeignKey(Order, verbose_name='变更订单', on_delete=models.PROTECT)
    proposed_total = money_field('变更后金额')
    proposed_due_date = models.DateField('变更后交期')
    order_version = models.PositiveIntegerField('申请时订单版本', editable=False)
    status = models.CharField('审批状态', max_length=20, default='pending', choices=[
        ('pending', '待审批'), ('approved', '已批准'), ('rejected', '已驳回')])


class AuditEvent(models.Model):
    module = 'audit'
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, verbose_name='操作人')
    object_type = models.CharField('业务模块', max_length=40)
    object_id = models.PositiveBigIntegerField('对象 ID')
    code = models.CharField('单据编号', max_length=32)
    action = models.CharField('操作', max_length=80)
    before = models.JSONField('变更前', default=dict)
    after = models.JSONField('变更后', default=dict)
    created_at = models.DateTimeField('操作时间', auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
