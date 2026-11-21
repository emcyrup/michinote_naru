from django.urls import path

from family import accounts

from . import views

app_name = 'familyapp'

# 職員が出す「パスワード設定のリンク」の行き先（family/accounts.py の SETUP_PATH と同じ形にする）
assert accounts.SETUP_PATH == '/account/setup/{token}/'

urlpatterns = [
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),
    path('account/setup/<str:token>/', views.SetupView.as_view(), name='setup'),
    path('manifest.webmanifest', views.manifest, name='manifest'),
    path('', views.HomeView.as_view(), name='home'),
    path('child/', views.ChildSelectView.as_view(), name='child'),
    path('book/from/', views.BookFromView.as_view(), name='book_from'),
    path('book/to/', views.BookToView.as_view(), name='book_to'),
    path('absence/', views.AbsenceView.as_view(), name='absence'),
    path('message/', views.MessageView.as_view(), name='message'),
    path('calendar/', views.CalendarView.as_view(), name='calendar'),
    path('calendar/day/<str:day>/', views.CalendarDayView.as_view(), name='calendar_day'),
    path('notice/', views.NoticeListView.as_view(), name='notice'),
    path('plan/', views.PlanView.as_view(), name='plan'),
    path('record/', views.RecordView.as_view(), name='record'),
]
