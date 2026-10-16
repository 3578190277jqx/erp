from dataclasses import dataclass
from . import models as m


@dataclass(frozen=True)
class Module:
    key: str
    model: type
    title: str
    singular: str
    description: str
    icon: str
    group: str
    columns: tuple
    fields: tuple
    creatable: bool = True


MODULES = {}


def register(key, model, title, singular, description, icon, group, columns, fields, creatable=True):
    MODULES[key] = Module(key, model, title, singular, description, icon, group, tuple(columns.split()), tuple(fields.split()), creatable)


register('customers', m.Customer, '客户与线索', '客户', '从第一次沟通，开始一段理想生活。', 'users', '客户与成交',
         'code name phone source status owner due_date', 'name phone address source due_date note')
register('measurements', m.Measurement, '量尺任务', '量尺任务', '每一毫米，都为更好的交付做准备。', 'ruler', '客户与成交',
         'code customer due_date area status owner', 'customer due_date area note')
register('designs', m.Design, '设计方案', '设计方案', '让空间的想象，有迹可循。', 'pen', '客户与成交',
         'code title customer revision status owner', 'customer title revision note')
register('quotes', m.Quote, '报价管理', '报价单', '面积、延米、套餐与费用，清晰算好每一笔。', 'file', '客户与成交',
         'code title customer total status owner', 'customer design title discount tax_rate note')
register('orders', m.Order, '合同与订单', '合同订单', '从签约到入住，每一步尽在掌握。', 'orders', '客户与成交',
         'code title customer total status due_date owner', 'customer title contract_number due_date total note')
register('breakdowns', m.Breakdown, '拆单与 BOM', '拆单任务', '将设计细节，变成精准的生产指令。', 'layers', '制造与供应',
         'code order quantity status owner', 'order quantity note')
register('materials', m.Material, '物料档案', '物料', '材料、规格与成本，统一管理。', 'box', '制造与供应',
         'code name specification unit unit_cost safety_stock', 'name specification unit unit_cost safety_stock note')
register('production', m.Production, '生产工单', '生产工单', '计划有序，进度清晰，品质如一。', 'factory', '制造与供应',
         'code breakdown work_center start_date due_date status owner', 'warehouse work_center start_date due_date note', False)
register('purchases', m.Purchase, '采购管理', '采购订单', '让合适的物料，在合适的时间到位。', 'cart', '制造与供应',
         'code supplier warehouse total due_date status', 'supplier_record supplier warehouse due_date note')
register('inventory', m.Stock, '库存查询', '库存', '每一件物料，都有准确的位置。', 'warehouse', '制造与供应',
         'code material warehouse quantity reserved available updated_at', '', False)
register('warehouses', m.Warehouse, '仓库管理', '仓库', '分仓管理，清晰追踪。', 'warehouse', '制造与供应',
         'code name address organization', 'name address note')
register('movements', m.StockMovement, '库存流水', '库存流水', '从收货到领料，每一次变化都有凭证。', 'activity', '制造与供应',
         'code material warehouse delta balance reference created_at', '', False)
register('deliveries', m.Delivery, '物流配送', '配送任务', '准时抵达，是承诺的一部分。', 'truck', '交付与服务',
         'code order due_date vehicle status owner', 'order due_date vehicle note')
register('installations', m.Installation, '安装派工', '安装任务', '让最后一公里，成为满意的开始。', 'tool', '交付与服务',
         'code order due_date status owner', 'order due_date note')
register('aftersales', m.AfterSales, '售后工单', '售后工单', '每一份反馈，都得到认真回应。', 'support', '交付与服务',
         'code title order priority due_date status owner', 'order title priority due_date note')
register('finance', m.Payment, '财务对账', '收付款', '资金往来清楚，经营心中有数。', 'wallet', '经营与设置',
         'code direction order purchase plan amount reference paid_date', '', False)
register('changes', m.ChangeRequest, '变更审批', '变更申请', '变化被看见，决定可追溯。', 'check', '经营与设置',
         'code order proposed_total proposed_due_date status owner', 'order proposed_total proposed_due_date note')
register('audit', m.AuditEvent, '操作日志', '审计事件', '谁，在什么时候，做了什么。', 'activity', '经营与设置',
         'code object_type action owner created_at', '', False)

READ_ONLY = {m.Stock, m.StockMovement, m.Payment, m.AuditEvent}
READ_ONLY |= {m.Handoff,m.PurchaseReceipt}

register('suppliers',m.Supplier,'供应商档案','供应商','联系人与账期，形成可靠的供应关系。','cart','制造与供应',
         'code name contact phone payment_days','name contact phone payment_days note')
register('followups',m.FollowUp,'客户跟进','跟进记录','每一次沟通都有记录，每一次约定都有提醒。','users','客户与成交',
         'code customer channel next_date owner created_at','customer channel next_date note')
register('handoffs',m.Handoff,'门厂交接','交接单','生产规格一次交接，工厂在独立业务域内接单履约。','factory','制造与供应',
         'code order target_organization status created_at','',False)
register('receipts',m.PurchaseReceipt,'采购收货凭证','收货凭证','记录每一批实际到货，追溯采购与库存。','warehouse','制造与供应',
         'code purchase reference created_at','',False)


def editable(obj):
    if type(obj) in READ_ONLY:
        return False
    if isinstance(obj, (m.Customer,m.Material,m.Warehouse,m.Supplier)):
        return True
    if isinstance(obj, m.Measurement) and obj.status == 'in_progress':
        return True
    return getattr(obj, 'status', None) in ('draft', 'rejected', 'pending', 'planned', 'assigned', 'failed', 'new')
