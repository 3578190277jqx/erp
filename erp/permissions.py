from django.core.exceptions import PermissionDenied
from django.db.models import Q

from .models import Membership, Organization

ALL_MODULES = {
    'customers', 'measurements', 'designs', 'quotes', 'orders', 'breakdowns', 'materials',
    'production', 'purchases', 'inventory', 'movements', 'deliveries', 'installations',
    'aftersales', 'finance', 'changes', 'audit', 'handoffs','receipts','suppliers','followups','attachments',
}
ROLE_READ = {
    'boss': ALL_MODULES, 'manager': ALL_MODULES,
    'sales': {'customers', 'measurements', 'designs', 'quotes', 'orders', 'deliveries', 'installations', 'changes'},
    'dealer': {'customers', 'measurements', 'designs', 'quotes', 'orders', 'deliveries', 'installations', 'changes'},
    'designer': {'customers', 'measurements', 'designs', 'quotes', 'orders', 'breakdowns', 'materials', 'inventory'},
    'surveyor': {'customers', 'measurements'},
    'engineer': {'designs', 'orders', 'breakdowns', 'production', 'materials', 'inventory'},
    'planner': {'orders', 'breakdowns', 'production', 'purchases', 'inventory', 'materials', 'deliveries'},
    'buyer': {'orders', 'breakdowns', 'production', 'purchases', 'materials', 'inventory'},
    'warehouse': {'orders', 'breakdowns', 'production', 'purchases', 'inventory', 'materials', 'movements', 'deliveries'},
    'logistics': {'customers', 'orders', 'production', 'inventory', 'deliveries'},
    'installer': {'customers', 'orders', 'installations'},
    'service': {'customers', 'orders', 'aftersales'},
    'finance': {'customers', 'orders', 'purchases', 'finance'},
}
ROLE_WRITE = {
    'boss': {'customers', 'measurements', 'designs', 'quotes', 'orders', 'deliveries', 'installations', 'aftersales', 'changes'},
    'manager': {'customers', 'measurements', 'designs', 'quotes', 'orders', 'deliveries', 'installations', 'aftersales', 'changes'},
    'sales': {'customers', 'quotes', 'orders', 'changes'}, 'dealer': {'customers', 'quotes', 'orders', 'changes'},
    'designer': {'designs', 'quotes'}, 'surveyor': {'measurements'},
    'engineer': {'breakdowns', 'materials'}, 'planner': {'production'},
    'buyer': {'purchases', 'materials'}, 'warehouse': {'inventory', 'materials'},
    'logistics': {'deliveries'}, 'installer': {'installations'}, 'service': {'aftersales'},
    'finance': {'finance'},
}


def role_allows(role, module, action):
    if module == 'receipts':
        return action in ('view','export') and 'purchases' in ROLE_READ.get(role,set())
    if module == 'suppliers':
        return 'purchases' in ROLE_READ.get(role,set()) if action in ('view','export') else role in ('boss','manager','buyer')
    if module == 'followups':
        return role_allows(role,'customers',action)
    if module == 'handoffs':
        if action=='withdraw':return role in ('boss','manager','sales','dealer')
        return role in ('boss','manager','sales','dealer','planner','engineer') if action in ('view','export') else role in ('boss','manager','planner')
    if action in ('view', 'export'):
        return module in ROLE_READ.get(role, set())
    if action in ('approve', 'reject'):
        return role in ('boss', 'manager') and module in ('quotes', 'designs', 'purchases', 'changes')
    if action == 'release':
        return role == 'engineer' and module == 'breakdowns'
    if action == 'reserve':
        return role in ('warehouse','planner') and module=='production'
    if action in ('receive', 'issue', 'adjust'):
        return role == 'warehouse' and module in ('purchases', 'production', 'inventory')
    if module in ('finance',) and action in ('create', 'edit'):
        return role == 'finance'
    return module in ROLE_WRITE.get(role, set())


def descendants(org_id):
    result, pending = {org_id}, {org_id}
    while pending:
        pending = set(Organization.objects.filter(parent_id__in=pending).values_list('id', flat=True)) - result
        result.update(pending)
    return result


def member_orgs(member):
    if not hasattr(member,'_scope_orgs'):
        member._scope_orgs=descendants(member.organization_id) if member.scope == 'tree' else {member.organization_id}
    return member._scope_orgs


def memberships(user):
    if hasattr(user,'_erp_memberships'):
        return user._erp_memberships
    return Membership.objects.filter(user=user).select_related('organization')


def can(user, module, action='view', organization=None):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    org_id = getattr(organization, 'pk', organization)
    return any(role_allows(m.role, module, action) and (org_id is None or org_id in member_orgs(m))
               for m in memberships(user))


def scoped(user, model, organization=None, action='view'):
    qs = model.objects.all()
    if not user.is_authenticated or not user.is_active:
        return qs.none()
    org_id = getattr(organization, 'pk', organization)
    if not user.is_superuser:
        predicate = Q(pk__in=[])
        for member in memberships(user):
            if not role_allows(member.role, model.module, action):
                continue
            condition = Q(organization_id__in=member_orgs(member))
            if member.scope == 'own':
                own = Q(owner=user)
                # A field worker must be able to read the parent of an assigned
                # task. Related access grants read/export only, never edit rights.
                paths = {
                    ('customers', 'surveyor'): ['measurement__owner'],
                    ('customers', 'designer'): ['design__owner', 'quote__owner'],
                    ('customers', 'installer'): ['order__installation__owner'],
                    ('customers', 'logistics'): ['order__delivery__owner'],
                    ('orders', 'installer'): ['installation__owner'],
                    ('orders', 'logistics'): ['delivery__owner'],
                    ('orders', 'designer'): ['customer__design__owner'],
                    ('measurements', 'sales'): ['customer__owner'],
                    ('measurements', 'dealer'): ['customer__owner'],
                    ('measurements', 'designer'): ['customer__design__owner'],
                    ('designs', 'sales'): ['customer__owner'],
                    ('designs', 'dealer'): ['customer__owner'],
                    ('quotes', 'designer'): ['design__owner'],
                    ('deliveries', 'sales'): ['order__owner'],
                    ('deliveries', 'dealer'): ['order__owner'],
                    ('installations', 'sales'): ['order__owner'],
                    ('installations', 'dealer'): ['order__owner'],
                }
                if action in ('view', 'export'):
                    for path in paths.get((model.module, member.role), []):
                        own |= Q(**{path: user})
                condition &= own
            predicate |= condition
        qs = qs.filter(predicate)
    if model.module == 'handoffs' and action in ('view','export'):
        incoming = accessible_organizations(user,'handoffs','view')
        qs = model.objects.filter(Q(pk__in=qs.values('pk')) | (Q(target_organization__in=incoming)&~Q(status='withdrawn')))
    if org_id:
        qs = qs.filter(Q(organization_id=org_id)|Q(target_organization_id=org_id)) if model.module=='handoffs' else qs.filter(organization_id=org_id)
    return qs.distinct()


def accessible_organizations(user, module=None, action='view'):
    if not user.is_authenticated:
        return Organization.objects.none()
    if user.is_superuser:
        return Organization.objects.all()
    ids = set()
    for member in memberships(user):
        if module is None or role_allows(member.role, module, action):
            ids.update(member_orgs(member))
    return Organization.objects.filter(pk__in=ids)


def require_access(user, obj, action):
    if not scoped(user, type(obj), action=action).filter(pk=obj.pk).exists():
        raise PermissionDenied('没有此单据的操作权限。')
