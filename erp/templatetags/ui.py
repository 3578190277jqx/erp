from decimal import Decimal
from django import template
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()
PATHS = {
    'grid': '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    'users': '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.87M16 3a4 4 0 0 1 0 8"/><circle cx="9" cy="7" r="4"/>',
    'ruler': '<path d="m3 15 12-12 6 6-12 12-6-6Zm5-5 3 3m1-7 3 3m1-7 3 3"/>',
    'pen': '<path d="m16 3 5 5-12 12-6 1 1-6L16 3Zm-2 2 5 5"/>',
    'file': '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8l-6-6Zm0 0v6h6M8 13h8m-8 4h5"/>',
    'orders': '<rect x="5" y="4" width="14" height="18" rx="2"/><path d="M9 2h6v4H9zM9 12h6m-6 5h6"/>',
    'layers': '<path d="m12 3 10 5-10 5L2 8l10-5ZM2 12l10 5 10-5M2 16l10 5 10-5"/>',
    'box': '<path d="m12 3 9 5v9l-9 5-9-5V8l9-5Zm0 19V12m-9-4 9 4 9-4M7 5l10 5"/>',
    'factory': '<path d="M3 21V9l6 3V7l6 4V3h4l2 18H3Zm4-5h1m4 0h1m4 0h1"/>',
    'cart': '<path d="M2 3h3l3 13h11l3-10H6M9 21h.01M19 21h.01"/>',
    'warehouse': '<path d="M3 21V8l9-5 9 5v13H3Zm5 0v-9h8v9M8 16h8"/>',
    'truck': '<path d="M1 3h14v13H1zM15 8h4l3 4v4h-7"/><circle cx="5" cy="18" r="3"/><circle cx="18" cy="18" r="3"/>',
    'tool': '<path d="M14.7 6.3a5 5 0 0 0-6.4 6.4L2 19l3 3 6.3-6.3a5 5 0 0 0 6.4-6.4L15 12l-3-3 2.7-2.7Z"/>',
    'support': '<path d="M3 14v-3a9 9 0 0 1 18 0v3M3 12H1v7h5v-7H3Zm18 0h2v7h-5v-7h3Zm0 7a4 4 0 0 1-4 4h-4"/>',
    'wallet': '<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 9V5l14-3v3m4 7h-6v4h6m-3-2h.01"/>',
    'check': '<path d="m8 12 3 3 5-6"/><rect x="3" y="3" width="18" height="18" rx="5"/>',
    'activity': '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
    'chart': '<path d="M3 3v18h18M7 14l4-4 4 3 6-7"/>',
    'plus': '<path d="M12 5v14M5 12h14"/>',
    'search': '<circle cx="10.5" cy="10.5" r="7"/><path d="m16 16 5 5"/>',
    'arrow': '<path d="M5 12h14m-5-5 5 5-5 5"/>',
    'chevron': '<path d="m9 5 7 7-7 7"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    'menu': '<path d="M4 6h16M4 12h16M4 18h16"/>',
    'bell': '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Zm-8 12h4"/>',
    'download': '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    'calendar': '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M16 3v4M8 3v4M3 11h18"/>',
    'logout': '<path d="M9 4H4v16h5m5-4 4-4-4-4m-5 4h13"/>',
    'settings': '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
}


@register.simple_tag
def icon(name):
    return format_html('<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>', mark_safe(PATHS.get(name, PATHS['file'])))


@register.filter
def money(value):
    return f'{Decimal(value or 0):,.2f}'


@register.filter
def tone(status):
    if status in ('completed', 'approved', 'received', 'delivered', 'accepted', 'won', 'resolved', 'released'):
        return 'green'
    if status in ('review', 'pending', 'new', 'draft', 'planned', 'assigned'):
        return 'amber'
    if status in ('failed', 'rejected', 'cancelled', 'lost'):
        return 'red'
    return 'blue'


@register.filter
def action_name(action):
    from erp.services.workflows import ACTION_LABELS
    labels = {'resend_handoff':'拒单后重发交接','reschedule_payment_plan':'同步分期到期日','delete_attachment':'删除附件','upgrade_change_review':'升级后重新核对变更','upgrade_internal_partner':'归并内部协作客户','upload_attachment':'上传私有附件','send_handoff':'发送工厂交接','accept_handoff':'工厂接单','reject_handoff':'工厂拒单','factory_progress':'工厂回传进度','factory_completed':'工厂完工回传','factory_order_created':'创建工厂生产订单','reserve_stock':'预留物料','release_reservation':'释放预留','partial_receipt':'登记分批收货','create_payment_plan':'启用分期回款','new_design_revision':'新建设计版本','import_csv':'批量导入','change_products':'修订产品快照','add_change_line':'添加变更增减项','followup':'更新跟进提醒','demo_upgrade_v11':'追加升级示例','create': '新建记录', 'edit': '编辑信息', 'add_line': '添加明细', 'delete_line': '移除明细',
              'stock_adjustment': '库存调整', 'record_payment': '登记收付款', 'export': '导出数据',
              'approved_change': '批准订单变更', 'quote_accepted': '报价成交',
              'production_started': '生产开工', 'production_completed': '生产完工',
              'delivery_shipped': '订单发运', 'delivery_received': '配送签收',
              'installation_completed': '安装验收', 'demo_create': '初始化示例记录', 'demo_seed': '演示初始化完成'}
    return labels.get(action, ACTION_LABELS.get(action, action))
