from django.urls import path

from . import views

app_name = 'family'

urlpatterns = [
    # 保護者（LINE から）
    path('enter/<str:token>/', views.EnterView.as_view(), name='enter'),
    path('', views.HomeView.as_view(), name='home'),
    path('child/', views.ChildSelectView.as_view(), name='child'),
    path('page/<slug:key>/', views.PageView.as_view(), name='page'),
    path('absence/', views.AbsenceView.as_view(), name='absence'),
    path('message/', views.MessageView.as_view(), name='message'),
    path('leave/', views.LeaveView.as_view(), name='leave'),
    # 職員（ログイン）
    path('staff/inbox/', views.StaffInboxView.as_view(), name='staff_inbox'),
    path('staff/inbox/<int:pk>/confirm/', views.StaffConfirmView.as_view(), name='staff_confirm'),
    path('staff/send-link/<int:guardian_pk>/', views.StaffSendLinkView.as_view(), name='staff_send_link'),
    path('staff/rotate-link/<int:guardian_pk>/', views.StaffRotateLinkView.as_view(), name='staff_rotate_link'),
]
