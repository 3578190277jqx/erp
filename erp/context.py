from collections import OrderedDict
from .permissions import accessible_organizations, can, memberships
from .registry import MODULES


def workspace(request):
    if not request.user.is_authenticated:
        return {}
    organizations = accessible_organizations(request.user)
    selected = request.session.get('organization_id')
    current = organizations.filter(pk=selected).first() if selected else None
    if selected and not current:
        request.session.pop('organization_id', None)
        selected = None
    groups = OrderedDict()
    for module in MODULES.values():
        if module.key in ('warehouses', 'movements','followups','receipts'):
            continue
        if can(request.user, module.model.module, 'view', selected):
            groups.setdefault(module.group, []).append(module)
    members = memberships(request.user)
    return {'nav_groups': groups.items(), 'workspace_orgs': organizations,
            'current_organization': current, 'active_organization': selected,
            'role_label': '系统管理员' if request.user.is_superuser else ' / '.join(dict.fromkeys(m.get_role_display() for m in members)),
            'user_display': request.user.first_name or request.user.username,
            'can_schedule': can(request.user, 'production', 'view', selected),
            'demo_mode': request.user.username.startswith('demo_')}
