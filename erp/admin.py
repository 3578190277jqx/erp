from django.contrib import admin
from .models import Organization, Membership, CollaborationRoute

admin.site.site_header = '栖木 ERP · 系统管理'
admin.site.site_title = '栖木 ERP'
admin.site.index_title = '组织、账号与权限'


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ['name', 'kind', 'parent','require_payment_plan']
    list_filter = ['kind']
    search_fields = ['name']


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ['user', 'organization', 'role', 'scope']
    list_filter = ['organization', 'role', 'scope']
    search_fields = ['user__username', 'user__first_name', 'organization__name']


@admin.register(CollaborationRoute)
class CollaborationRouteAdmin(admin.ModelAdmin):
    list_display=['source','target','enabled']
    list_filter=['enabled','source','target']
