from django.urls import path

from . import views

app_name = 'shifts'

urlpatterns = [
    path('', views.MonthView.as_view(), name='month'),
    path('print/', views.PrintView.as_view(), name='print'),
    path('setup/', views.SetupView.as_view(), name='setup'),
]
