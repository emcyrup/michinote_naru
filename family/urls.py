from django.urls import path

from . import views

app_name = 'family'

urlpatterns = [
    # 保護者（LINE から）
    path('enter/<str:token>/', views.EnterView.as_view(), name='enter'),
    path('', views.HomeView.as_view(), name='home'),
    path('child/', views.ChildSelectView.as_view(), name='child'),
    path('page/<slug:key>/', views.PageView.as_view(), name='page'),
    path('leave/', views.LeaveView.as_view(), name='leave'),
    # 職員（ログイン）
    path('staff/send-link/<int:guardian_pk>/', views.StaffSendLinkView.as_view(), name='staff_send_link'),
    path('staff/rotate-link/<int:guardian_pk>/', views.StaffRotateLinkView.as_view(), name='staff_rotate_link'),
]
