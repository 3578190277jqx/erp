from django.contrib import admin
from django.contrib.auth import views as auth
from django.urls import path
from erp.views import modules as v, workspace as w
from erp.views import features as f
from erp.views.health import health

urlpatterns = [
    path('healthz/',health,name='health'),
    path('admin/', admin.site.urls),
    path('login/', auth.LoginView.as_view(template_name='erp/login.html'), name='login'),
    path('logout/', auth.LogoutView.as_view(), name='logout'),
    path('', w.dashboard, name='dashboard'),
    path('reports/', w.reports, name='reports'),
    path('schedule/', w.schedule, name='schedule'),
    path('organizations/', w.organizations, name='organizations'),
    path('organizations/switch/', w.switch_organization, name='switch_organization'),
    path('inventory/adjust/', v.inventory_adjust, name='inventory_adjust'),
    path('finance/order-plans/',f.payment_plan_choices,name='payment_plan_choices'),
    path('modules/<slug:key>/', v.list_records, name='list'),
    path('modules/<slug:key>/new/', v.edit_record, name='create'),
    path('modules/<slug:key>/export/', v.export, name='export'),
    path('modules/<slug:key>/batch/', v.batch, name='batch'),
    path('modules/<slug:key>/<int:pk>/', v.detail, name='detail'),
    path('modules/<slug:key>/<int:pk>/edit/', v.edit_record, name='edit'),
    path('modules/<slug:key>/<int:pk>/action/', v.act, name='action'),
    path('modules/<slug:key>/<int:pk>/lines/', v.lines, name='lines'),
    path('modules/<slug:key>/<int:pk>/print/',f.print_record,name='print_record'),
    path('features/<slug:key>/<int:pk>/<slug:operation>/',f.feature_action,name='feature_action'),
    path('attachments/<slug:key>/<int:pk>/upload/',f.upload,name='upload'),
    path('attachments/<int:pk>/download/',f.download,name='download'),
    path('attachments/<int:pk>/delete/',f.delete_attachment,name='delete_attachment'),
    path('imports/<slug:key>/',f.import_csv,name='import_csv'),
]
