"""One immutable description of document rows and their editor boundaries."""
from dataclasses import dataclass
from types import MappingProxyType
from . import models as m


@dataclass(frozen=True)
class LineSpec:
    model: type
    parent_field: str
    parent_label: str
    snapshot_label: str = '明细快照'
    generic_editor: bool = True
    fields: tuple[str, ...] = ()


LINE_SPECS = MappingProxyType({
    m.Quote:LineSpec(m.QuoteLine,'quote','报价单',fields=(
        'name','room','specification','category','pricing_mode','width_mm','height_mm',
        'quantity','unit','unit_price','unit_cost','discountable')),
    m.Purchase:LineSpec(m.PurchaseLine,'purchase','采购订单',fields=('material','quantity','unit_price')),
    m.Breakdown:LineSpec(m.BOMLine,'breakdown','拆单任务',fields=('order_line','material','quantity','waste')),
    m.ChangeRequest:LineSpec(m.ChangeLine,'change','变更申请','申请明细快照',False),
})
ROW_SPECS = MappingProxyType({spec.model:spec for spec in LINE_SPECS.values()})
