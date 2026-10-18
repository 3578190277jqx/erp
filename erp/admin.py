from django.contrib import admin
from .models import Organization, Membership, CollaborationRoute, EvidenceState

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


@admin.register(EvidenceState)
class EvidenceStateAdmin(admin.ModelAdmin):
    list_display=['organization','object_type','object_id','epoch','last_changed']
    list_filter=['organization','object_type']
    search_fields=['object_type','=object_id']
    list_select_related=['organization']
    readonly_fields=['organization','object_type','object_id','epoch','last_changed']
    fields=readonly_fields
    actions=None

    @admin.display(description='最近附件变更时间',ordering='updated_at')
    def last_changed(self,obj):
        return obj.updated_at or '历史时间未记录'

    def has_module_permission(self,request):return request.user.is_active and request.user.is_superuser
    def has_view_permission(self,request,obj=None):return self.has_module_permission(request)
    def has_add_permission(self,request):return False
    def has_change_permission(self,request,obj=None):return False
    def has_delete_permission(self,request,obj=None):return False
