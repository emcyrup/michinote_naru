from django.urls import path

from . import views

app_name = 'family'

urlpatterns = [
    # 保護者（LINE から）
    path('enter/<str:token>/', views.EnterView.as_view(), name='enter'),
    path('', views.HomeView.as_view(), name='home'),
    path('child/', views.ChildSelectView.as_view(), name='child'),
    path('page/<slug:key>/', views.PageView.as_view(), name='page'),
    path('from/', views.BookFromView.as_view(), name='book_from'),
    path('to/', views.BookToView.as_view(), name='book_to'),
    path('absence/', views.AbsenceView.as_view(), name='absence'),
    path('message/', views.MessageView.as_view(), name='message'),
    path('leave/', views.LeaveView.as_view(), name='leave'),
    # 職員（ログイン）
    path('staff/book/', views.StaffBookListView.as_view(), name='staff_book'),
    path('staff/book/<int:beneficiary_pk>/', views.StaffBookEditView.as_view(), name='staff_book_edit'),
    path('staff/inbox/', views.StaffInboxView.as_view(), name='staff_inbox'),
    path('staff/inbox/<int:pk>/confirm/', views.StaffConfirmView.as_view(), name='staff_confirm'),
    path('staff/send-link/<int:guardian_pk>/', views.StaffSendLinkView.as_view(), name='staff_send_link'),
    path('staff/rotate-link/<int:guardian_pk>/', views.StaffRotateLinkView.as_view(), name='staff_rotate_link'),
]
